"""Sidecar process supervision, ported from src-tauri/src/main.rs.

Spawns the desktop backend (compiled `code-sidecar` binary, or
`bun run sidecar/index.ts` as a fallback), waits for its JSON "ready" line on
stdout, restarts it if it crashes, and shuts it down gracefully via
POST /shutdown.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import AppPaths, find_bun, find_sidecar_binary

log = logging.getLogger("cline_desktop.sidecar")

READY_TIMEOUT_SECONDS = 15
SHUTDOWN_GRACE_SECONDS = 7
HEALTH_INTERVAL_SECONDS = 5


class SidecarError(RuntimeError):
    pass


class SidecarSupervisor:
    """Owns the sidecar process for the lifetime of the app.

    The approval token and preferred port are pinned via env so a crash
    restart comes back on the same ws endpoint and the webview's cached
    endpoint (with its approval token) keeps working.
    """

    def __init__(
        self,
        paths: AppPaths,
        env: dict[str, str],
        sidecar_bin: Path | None = None,
    ) -> None:
        self._paths = paths
        self._env = env
        self._explicit_bin = sidecar_bin
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._shutting_down = False
        self.ws_endpoint: str | None = None
        self.http_endpoint: str | None = None

    # -- spawning ----------------------------------------------------------

    def _resolve_command(self) -> tuple[list[str], Path]:
        """Return (argv, cwd). Mirrors resolve_desktop_backend_binary_path."""
        if self._explicit_bin:
            if not self._explicit_bin.is_file():
                raise SidecarError(f"Sidecar binary not found: {self._explicit_bin}")
            return [str(self._explicit_bin)], self._paths.desktop_app_dir

        compiled = find_sidecar_binary(self._paths.desktop_app_dir)
        if compiled:
            return [str(compiled)], self._paths.desktop_app_dir

        bun = find_bun()
        if not bun:
            raise SidecarError(
                "No compiled sidecar binary found and bun is not installed. "
                "Either run `bun run build:sidecar:bin` in apps/examples/desktop-app "
                "or install bun (https://bun.sh)."
            )
        entry = self._paths.desktop_app_dir / "sidecar" / "index.ts"
        return [bun, "run", str(entry)], self._paths.workspace_root

    def start(self) -> None:
        with self._lock:
            self._spawn_locked()
        if not self._ready.wait(READY_TIMEOUT_SECONDS):
            self.stop()
            raise SidecarError(
                f"Sidecar did not report ready within {READY_TIMEOUT_SECONDS}s"
            )
        threading.Thread(target=self._health_loop, daemon=True, name="sidecar-health").start()

    def _spawn_locked(self) -> None:
        argv, cwd = self._resolve_command()
        log.info("Spawning sidecar: %s (cwd=%s)", " ".join(argv), cwd)
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        self._ready.clear()
        self._proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kwargs,
        )
        threading.Thread(
            target=self._drain_stdout, args=(self._proc,), daemon=True, name="sidecar-stdout"
        ).start()
        threading.Thread(
            target=self._drain_stderr, args=(self._proc,), daemon=True, name="sidecar-stderr"
        ).start()

    # -- pipe draining -----------------------------------------------------

    def _drain_stdout(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if not self._ready.is_set():
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and payload.get("type") == "ready":
                    self.ws_endpoint = payload.get("wsEndpoint") or payload.get("endpoint")
                    self.http_endpoint = payload.get("endpoint")
                    log.info("Sidecar ready: %s (pid=%s)", self.ws_endpoint, payload.get("pid"))
                    self._ready.set()
                    continue
            log.info("[desktop-backend] %s", line)

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                log.warning("[desktop-backend:err] %s", line)

    # -- health / restart --------------------------------------------------

    def _health_loop(self) -> None:
        while not self._shutting_down:
            time.sleep(HEALTH_INTERVAL_SECONDS)
            if self._shutting_down:
                return
            with self._lock:
                proc = self._proc
                if proc is None or proc.poll() is None:
                    continue
                log.warning("Sidecar exited (code=%s); restarting", proc.returncode)
                previous_endpoint = self.ws_endpoint
                try:
                    self._spawn_locked()
                except SidecarError as err:
                    log.error("Failed to respawn sidecar: %s", err)
                    return
            if self._ready.wait(READY_TIMEOUT_SECONDS):
                if previous_endpoint and self.ws_endpoint != previous_endpoint:
                    # Port/token are pinned via env, so this only happens if the
                    # preferred port got stolen while we were down.
                    log.warning(
                        "Sidecar endpoint changed after restart (%s -> %s); "
                        "the webview may need a reload",
                        previous_endpoint,
                        self.ws_endpoint,
                    )
            else:
                log.error("Restarted sidecar did not become ready")

    # -- shutdown ----------------------------------------------------------

    def stop(self) -> None:
        self._shutting_down = True
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None or proc.poll() is not None:
            return
        if self.http_endpoint:
            try:
                req = urllib.request.Request(
                    f"{self.http_endpoint}/shutdown", method="POST", data=b""
                )
                urllib.request.urlopen(req, timeout=2)
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log.info("Sidecar exited gracefully")
                return
            time.sleep(0.1)
        log.warning("Sidecar did not exit after /shutdown; killing")
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
