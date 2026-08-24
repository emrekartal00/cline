/**
 * Node.js fallback for `Bun.serve` with the subset of the API the sidecar
 * uses (fetch handler + websocket open/message/close + `server.upgrade`).
 *
 * Lets the sidecar run under plain Node (>=22, for WHATWG Request/Response
 * globals) when no Bun runtime or compiled binary is available — e.g. from
 * the single-file `bun build --target=node` bundle used by the Python
 * desktop shell on machines where shipping executables is not an option.
 */

import type { IncomingMessage, ServerResponse } from "node:http";
import { createServer } from "node:http";
import type { Duplex } from "node:stream";
import { Readable } from "node:stream";
import { WebSocketServer, type WebSocket as NodeWebSocket } from "ws";

type WebSocketWrapper = {
	data: unknown;
	send: (message: string) => void;
	close: (code?: number, reason?: string) => void;
};

type WebSocketHandler = {
	maxPayloadLength?: number;
	open?: (ws: WebSocketWrapper) => void;
	message?: (ws: WebSocketWrapper, raw: string) => void | Promise<void>;
	close?: (ws: WebSocketWrapper) => void;
};

type NodeServeOptions = {
	hostname: string;
	port: number;
	fetch: (
		req: Request,
		server: { port: number; upgrade: (req: Request, opts?: { data?: unknown }) => boolean },
	) => Promise<Response | undefined> | Response | undefined;
	websocket: WebSocketHandler;
};

function toRequest(req: IncomingMessage, hostname: string): Request {
	const url = `http://${req.headers.host ?? hostname}${req.url ?? "/"}`;
	const headers = new Headers();
	for (const [key, value] of Object.entries(req.headers)) {
		if (typeof value === "string") headers.set(key, value);
		else if (Array.isArray(value)) headers.set(key, value.join(", "));
	}
	const method = req.method ?? "GET";
	const hasBody = method !== "GET" && method !== "HEAD";
	return new Request(url, {
		method,
		headers,
		body: hasBody ? (Readable.toWeb(req) as unknown as BodyInit) : undefined,
		// @ts-expect-error half-duplex is required for streamed request bodies
		duplex: "half",
	});
}

async function writeResponse(res: ServerResponse, response: Response): Promise<void> {
	res.statusCode = response.status;
	response.headers.forEach((value, key) => res.setHeader(key, value));
	const body = Buffer.from(await response.arrayBuffer());
	res.end(body);
}

export async function nodeServe(
	options: NodeServeOptions,
): Promise<{ port: number; stop: () => void }> {
	const { hostname, port, fetch: fetchHandler, websocket } = options;
	const wss = new WebSocketServer({
		noServer: true,
		maxPayload: websocket.maxPayloadLength,
	});

	const httpServer = createServer(async (req, res) => {
		try {
			const response = await fetchHandler(toRequest(req, hostname), {
				get port() {
					const address = httpServer.address();
					return typeof address === "object" && address ? address.port : 0;
				},
				// Plain HTTP requests never upgrade.
				upgrade: () => false,
			});
			if (response) {
				await writeResponse(res, response);
			} else {
				res.statusCode = 404;
				res.end();
			}
		} catch {
			res.statusCode = 500;
			res.end();
		}
	});

	httpServer.on(
		"upgrade",
		async (rawReq: IncomingMessage, socket: Duplex, head: Buffer) => {
			let upgraded = false;
			let upgradeData: unknown;
			let response: Response | undefined;
			try {
				response = await fetchHandler(toRequest(rawReq, hostname), {
					get port() {
						const address = httpServer.address();
						return typeof address === "object" && address ? address.port : 0;
					},
					upgrade: (_req, opts) => {
						upgraded = true;
						upgradeData = opts?.data;
						return true;
					},
				});
			} catch {
				socket.destroy();
				return;
			}
			if (!upgraded) {
				const status = response?.status ?? 403;
				socket.write(`HTTP/1.1 ${status} Forbidden\r\nConnection: close\r\n\r\n`);
				socket.destroy();
				return;
			}
			wss.handleUpgrade(rawReq, socket, head, (client: NodeWebSocket) => {
				const wrapper: WebSocketWrapper = {
					data: upgradeData,
					send: (message) => client.send(message),
					close: (code, reason) => client.close(code, reason),
				};
				websocket.open?.(wrapper);
				client.on("message", (raw) => {
					void websocket.message?.(wrapper, raw.toString());
				});
				client.on("close", () => websocket.close?.(wrapper));
			});
		},
	);

	await new Promise<void>((resolve, reject) => {
		let retriedOnEphemeralPort = false;
		const onError = (error: NodeJS.ErrnoException) => {
			if (error.code === "EADDRINUSE" && port !== 0 && !retriedOnEphemeralPort) {
				retriedOnEphemeralPort = true;
				httpServer.listen(0, hostname);
				return;
			}
			reject(error);
		};
		httpServer.on("error", onError);
		httpServer.once("listening", () => {
			httpServer.off("error", onError);
			resolve();
		});
		httpServer.listen(port, hostname);
	});

	const address = httpServer.address();
	const boundPort = typeof address === "object" && address ? address.port : 0;
	return {
		port: boundPort,
		stop: () => {
			wss.close();
			httpServer.close();
		},
	};
}
