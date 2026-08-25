import { homedir } from "node:os";
import {
	createClineTelemetryServiceConfig,
	setHomeDirIfUnset,
	watchManagedHubBuildMismatch,
} from "@cline/core";
import { captureSdkError, claimHubDaemonProcess } from "@cline/shared";
import { prewarmWorkspaceMetadata } from "./chat-session";
import { configureConnectorCliLaunch } from "./connectors";
import {
	broadcastEvent,
	createSidecarContext,
	disposeSidecarContext,
	initializeSessionManager,
} from "./context";
import { createDesktopObservability } from "./observability";
import { resolveWorkspaceRoot } from "./paths";
import { startServer } from "./server";
import { ensureLoginShellPath } from "./shell-path";
import { buildTelemetrySelfcheckReport } from "./telemetry-selfcheck";
import { BunRuntime, SIDECAR_HOST, SIDECAR_MODE, SIDECAR_PORT } from "./types";

const SHUTDOWN_TIMEOUT_MS = 5_000;
let activeObservability:
	| ReturnType<typeof createDesktopObservability>
	| undefined;

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
	let timeout: ReturnType<typeof setTimeout> | undefined;
	return Promise.race([
		promise,
		new Promise<T>((_, reject) => {
			timeout = setTimeout(
				() => reject(new Error(`shutdown timed out after ${timeoutMs}ms`)),
				timeoutMs,
			);
		}),
	]).finally(() => {
		if (timeout) {
			clearTimeout(timeout);
		}
	});
}

async function main() {
	if (!BunRuntime && !process.versions.node) {
		throw new Error("sidecar requires Bun or Node.js (>=22)");
	}

	// Disable TLS certificate verification for outgoing requests when
	// CLINE_SIDECAR_INSECURE_TLS=1. Needed for enterprise endpoints with a
	// self-signed or internal-CA certificate. Node's global fetch (undici,
	// used by the LLM providers) ignores NODE_TLS_REJECT_UNAUTHORIZED, so we
	// install a global undici dispatcher; the env var covers node:https paths.
	if (process.env.CLINE_SIDECAR_INSECURE_TLS === "1") {
		process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
		try {
			const { setGlobalDispatcher, Agent } = await import("undici");
			setGlobalDispatcher(new Agent({ connect: { rejectUnauthorized: false } }));
			console.error(
				"[cline-sidecar] WARNING: TLS certificate verification disabled " +
					"(CLINE_SIDECAR_INSECURE_TLS=1)",
			);
		} catch (error) {
			console.error("[cline-sidecar] Failed to disable TLS verification:", error);
		}
	}

	// Strip the `model` field from outgoing chat/completions requests when
	// CLINE_SIDECAR_OMIT_MODEL=1. Needed for single-model endpoints (e.g. a
	// corporate GGUF/llama.cpp server) that reject any request carrying a
	// `model` field — the endpoint *is* the model. Whatever model id is
	// configured (including the gpt-4o default) is removed before it hits the
	// wire. Only touches JSON bodies on *completions paths.
	if (process.env.CLINE_SIDECAR_OMIT_MODEL === "1") {
		const originalFetch = globalThis.fetch;
		globalThis.fetch = (async (
			input: Parameters<typeof fetch>[0],
			init?: Parameters<typeof fetch>[1],
		) => {
			try {
				const url =
					typeof input === "string"
						? input
						: input instanceof URL
							? input.href
							: (input as Request).url;
				if (
					init?.body &&
					typeof init.body === "string" &&
					/\/(chat\/)?completions(\?|$)/.test(url)
				) {
					const parsed = JSON.parse(init.body);
					if (parsed && typeof parsed === "object" && "model" in parsed) {
						delete parsed.model;
						init = { ...init, body: JSON.stringify(parsed) };
					}
				}
			} catch {
				// Any parse/inspection failure: send the request unchanged.
			}
			return originalFetch(input, init);
		}) as typeof fetch;
		console.error(
			"[cline-sidecar] Omitting `model` from chat/completions requests " +
				"(CLINE_SIDECAR_OMIT_MODEL=1)",
		);
	}

	// When launched from Finder/the Dock the app inherits launchd's minimal
	// PATH, so agent-spawned processes can't find shell-profile-installed
	// tools like `gh`. Kick resolution off first so it overlaps the rest of
	// startup, but await it before the session manager exists — that's what
	// spawns children (agent sessions, MCP servers, scheduled runs).
	const shellPathPromise = ensureLoginShellPath();

	const workspaceRoot = resolveWorkspaceRoot(process.cwd());
	setHomeDirIfUnset(homedir());
	configureConnectorCliLaunch(workspaceRoot);
	const observability = createDesktopObservability();
	activeObservability = observability;
	const ctx = createSidecarContext(workspaceRoot, observability);
	observability.logger.log("Desktop sidecar starting", {
		workspaceRoot,
		pid: process.pid,
	});

	prewarmWorkspaceMetadata(workspaceRoot);
	observability.logger.log(
		"Login shell PATH resolution",
		await shellPathPromise,
	);
	await initializeSessionManager(ctx);

	let shuttingDown = false;
	let handlingFatalError = false;
	const shutdown = async (reason = "code_sidecar_shutdown"): Promise<void> => {
		if (shuttingDown) {
			return;
		}
		shuttingDown = true;
		observability.logger.log("Desktop sidecar shutting down", { reason });
		await withTimeout(
			(async () => {
				try {
					await disposeSidecarContext(ctx, reason);
				} finally {
					await observability.dispose();
				}
			})(),
			SHUTDOWN_TIMEOUT_MS,
		);
	};

	const shutdownAndExit = (signal: string): void => {
		void shutdown(`code_sidecar_${signal.toLowerCase()}`).finally(() => {
			process.exit(signal === "SIGINT" ? 130 : 143);
		});
	};

	process.once("SIGINT", () => shutdownAndExit("SIGINT"));
	process.once("SIGTERM", () => shutdownAndExit("SIGTERM"));
	const handleFatalError = (kind: string, error: unknown): void => {
		if (handlingFatalError) {
			process.exit(1);
		}
		handlingFatalError = true;
		observability.logger.error?.("Desktop sidecar process error", {
			kind,
			error,
		});
		captureSdkError(observability.telemetry, {
			component: "desktop",
			operation: `sidecar.${kind}`,
			error,
			handled: false,
			severity: "fatal",
		});
		void shutdown(`code_sidecar_${kind}`).finally(() => process.exit(1));
	};
	process.on("uncaughtException", (error) => {
		handleFatalError("uncaught_exception", error);
	});
	process.on("unhandledRejection", (error) => {
		handleFatalError("unhandled_rejection", error);
	});
	process.once("beforeExit", () => {
		void shutdown("code_sidecar_before_exit");
	});

	const { port, approvalToken } = await startServer(ctx, SIDECAR_PORT, shutdown);
	observability.logger.log("Desktop sidecar ready", {
		port,
		mode: SIDECAR_MODE,
	});

	// Another Cline installation (e.g. an updated CLI) can replace the shared
	// Hub daemon while this app is running. Surface that to the webview so it
	// can prompt the user to update and restart.
	watchManagedHubBuildMismatch({
		onMismatch: (mismatch) => {
			ctx.hubBuildMismatch = mismatch;
			observability.logger.log("Managed hub build mismatch detected", {
				hubBuildId: mismatch.hubBuildId,
				hubCoreVersion: mismatch.hubCoreVersion,
				reason: mismatch.reason,
			});
			broadcastEvent(ctx, "hub_build_mismatch", mismatch);
		},
	});

	// A wildcard bind isn't a dialable address; advertise loopback instead.
	const dialHost = SIDECAR_HOST === "0.0.0.0" ? "127.0.0.1" : SIDECAR_HOST;
	const endpoint = `http://${dialHost}:${port}`;
	const wsEndpoint = new URL(`ws://${dialHost}:${port}/transport`);
	wsEndpoint.searchParams.set("approval_token", approvalToken);
	process.stdout.write(
		`${JSON.stringify({
			type: "ready",
			endpoint,
			wsEndpoint: wsEndpoint.toString(),
			pid: process.pid,
			mode: SIDECAR_MODE,
		})}\n`,
	);
}

/**
 * Prints whether the telemetry configuration that was inlined at build time
 * (see scripts/telemetry-define-args.ts) actually made it into this binary,
 * then exits. CI runs this against the packaged sidecar and fails the
 * publish when a release-grade build reports `"enabled":false` or an
 * unusable OTLP endpoint, so a regression in the build-time inlining can
 * never ship silently again.
 */
function runTelemetrySelfcheck(): void {
	const report = buildTelemetrySelfcheckReport(
		createClineTelemetryServiceConfig(),
	);
	process.stdout.write(`${JSON.stringify(report)}\n`);
}

async function runEntrypoint(): Promise<void> {
	// Before the daemon-sentinel claim: the selfcheck only inspects build-time
	// config and must not consume the sentinel or start anything.
	if (process.argv.includes("--telemetry-selfcheck")) {
		runTelemetrySelfcheck();
		return;
	}
	// Claim rather than read: consuming the sentinel keeps daemon-hosted sessions
	// from handing it to every process they spawn.
	if (claimHubDaemonProcess()) {
		await import("@cline/core/hub/daemon-entry");
		return;
	}
	await main();
}

runEntrypoint().catch(async (error) => {
	const message = error instanceof Error ? error.message : String(error);
	activeObservability?.logger.error?.("Desktop sidecar process failed", {
		error,
	});
	captureSdkError(activeObservability?.telemetry, {
		component: "desktop",
		operation: "sidecar.startup",
		error,
		handled: false,
		severity: "fatal",
	});
	await activeObservability?.dispose();
	process.stderr.write(`${message}\n`);
	process.exit(1);
});
