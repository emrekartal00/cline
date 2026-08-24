"""Application orchestration: sidecar + UI server + pywebview window."""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .bootstrap import build_injection
from .bridge import Bridge
from .config import (
    DEFAULT_SIDECAR_PORT,
    DEFAULT_UI_PORT,
    AppPaths,
    find_bun,
    is_port_free,
    pick_free_port,
)
from .sidecar import SidecarError, SidecarSupervisor
from .webserver import StaticWebviewServer

log = logging.getLogger("cline_desktop.app")

DEV_SERVER_TIMEOUT_SECONDS = 120


@dataclass
class Options:
    dev: bool = False
    attach: bool = False
    no_window: bool = False
    debug: bool = False
    sidecar_bin: Path | None = None
    sidecar_port: int | None = None
    ui_port: int | None = None


class App:
    def __init__(self, options: Options) -> None:
        self.options = options
        self.paths = AppPaths.discover()
        self.approval_token = uuid.uuid4().hex
        self.sidecar: SidecarSupervisor | None = None
        self.web_server: StaticWebviewServer | None = None
        self.dev_server: subprocess.Popen | None = None
        self._cleaned_up = False

    # -- port / env selection ------------------------------------------------

    def _choose_ui_port(self) -> int:
        if self.options.ui_port:
            return self.options.ui_port
        if is_port_free(DEFAULT_UI_PORT):
            return DEFAULT_UI_PORT
        port = pick_free_port()
        log.warning("Port %d busy; serving UI on %d instead", DEFAULT_UI_PORT, port)
        return port

    def _choose_sidecar_port(self) -> int:
        if self.options.sidecar_port:
            return self.options.sidecar_port
        if is_port_free(DEFAULT_SIDECAR_PORT):
            return DEFAULT_SIDECAR_PORT
        return pick_free_port()

    def _sidecar_env(self, ui_port: int) -> dict[str, str]:
        env = dict(os.environ)
        # Pin the token and port so crash restarts reuse the same ws endpoint
        # (the webview caches the resolved endpoint including the token).
        env["CLINE_SIDECAR_APPROVAL_TOKEN"] = self.approval_token
        env["CLINE_SIDECAR_PORT"] = str(self._chosen_sidecar_port)
        # Run the agent in-process (no separate Hub daemon). Hub mode needs to
        # spawn a runtime for the daemon, which fails on locked-down machines
        # ("No compatible hub runtime is available"). Local mode is
        # self-contained. Override with CLINE_SIDECAR_BACKEND_MODE if needed.
        env.setdefault("CLINE_SIDECAR_BACKEND_MODE", "local")
        if ui_port not in (DEFAULT_UI_PORT,):
            # Non-default UI port is not in the sidecar's built-in Origin
            # allowlist; register it or the WS upgrade is refused.
            extra = f"http://127.0.0.1:{ui_port},http://localhost:{ui_port}"
            existing = env.get("CLINE_SIDECAR_TRUSTED_ORIGINS", "").strip()
            env["CLINE_SIDECAR_TRUSTED_ORIGINS"] = f"{existing},{extra}" if existing else extra
        return env

    # -- startup ---------------------------------------------------------------

    def start(self) -> str:
        """Start backend + UI. Returns the URL the window should load."""
        ui_port = self._choose_ui_port()
        self._chosen_sidecar_port = self._choose_sidecar_port()

        self.sidecar = SidecarSupervisor(
            self.paths,
            env=self._sidecar_env(ui_port),
            sidecar_bin=self.options.sidecar_bin,
        )
        self.sidecar.start()
        assert self.sidecar.ws_endpoint, "sidecar ready without ws endpoint"

        if self.options.dev:
            url = f"http://localhost:{ui_port}"
            if not self.options.attach:
                self._start_dev_server(ui_port)
            self._wait_for_http(url, DEV_SERVER_TIMEOUT_SECONDS)
            return url

        self.web_server = StaticWebviewServer(
            root=self.paths.webview_out,
            port=ui_port,
            injection=build_injection(self.sidecar.ws_endpoint),
        )
        self.web_server.start()
        return self.web_server.url

    def _start_dev_server(self, ui_port: int) -> None:
        bun = find_bun()
        if not bun:
            raise SidecarError("--dev requires bun (https://bun.sh) to run the Next dev server")
        env = dict(os.environ)
        # The Next dev server can't have HTML injected, so use the env-var
        # endpoint path (priority 3 in desktop-client.ts) instead.
        env["NEXT_PUBLIC_SIDECAR_WS_ENDPOINT"] = self.sidecar.ws_endpoint or ""
        log.info("Starting Next dev server on port %d", ui_port)
        self.dev_server = subprocess.Popen(
            [bun, "run", "next", "dev", "webview", "-p", str(ui_port), "--turbo"],
            cwd=str(self.paths.desktop_app_dir),
            env=env,
        )

    @staticmethod
    def _wait_for_http(url: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(url, timeout=2)
                return
            except (urllib.error.URLError, OSError, TimeoutError):
                time.sleep(0.5)
        raise TimeoutError(f"UI server at {url} did not come up within {timeout:.0f}s")

    # -- window / run loop ------------------------------------------------------

    def run(self) -> int:
        self._install_signal_handlers()
        atexit.register(self.cleanup)
        try:
            url = self.start()
        except (SidecarError, FileNotFoundError, TimeoutError) as err:
            log.error("%s", err)
            self.cleanup()
            return 1

        if self.options.no_window:
            return self._run_in_browser(url)

        # Try the native window; if the platform's webview can't start (e.g.
        # Windows without the WebView2 runtime, where pywebview falls back to
        # the legacy IE renderer and hits registry/policy restrictions), fall
        # back to opening the UI in the default browser instead of crashing.
        try:
            import webview  # deferred: heavy import; browser mode needs no GUI libs

            window = webview.create_window(
                "Cline",
                url,
                width=1280,
                height=800,
                min_size=(900, 600),
                js_api=Bridge(),
            )
            window.events.closed += self.cleanup
            # private_mode=False is load-bearing: the webview persists provider
            # settings and UI state in localStorage, which private mode wipes.
            webview.start(private_mode=False, debug=self.options.debug)
            self.cleanup()
            return 0
        except Exception as err:  # noqa: BLE001 - any GUI failure -> browser
            log.warning("Native window unavailable (%s); opening in browser.", err)
            return self._run_in_browser(url)

    def _run_in_browser(self, url: str) -> int:
        import webbrowser

        webbrowser.open(url)
        log.info("Cline is running at %s — open it in your browser. Ctrl-C to stop.", url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.cleanup()
        return 0

    # -- teardown ---------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum, _frame):
            log.info("Received signal %s; shutting down", signum)
            self.cleanup()
            sys.exit(0)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self.dev_server and self.dev_server.poll() is None:
            self.dev_server.terminate()
            try:
                self.dev_server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.dev_server.kill()
        if self.web_server:
            self.web_server.stop()
        if self.sidecar:
            self.sidecar.stop()
        log.info("Shutdown complete")
