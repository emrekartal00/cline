#!/usr/bin/env python3
"""One-click launcher for the Cline Python desktop app.

Just run this file — from a terminal, Spyder, VS Code, or a double-click
launcher. No `pip install -e .` or venv activation required:

- adds src/ to sys.path so the package imports from the checkout,
- installs pywebview into the running interpreter if it's missing,
- opens a native window when possible; inside an IPython console (Spyder,
  Jupyter) — where a native window can't own the main thread — it runs
  headless and opens the UI in your default browser instead.

Stop it with Ctrl-C (or Spyder's stop button); the backend shuts down
cleanly either way.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

UI_URL = "http://127.0.0.1:3125"


def running_in_ipython() -> bool:
    try:
        get_ipython  # type: ignore[name-defined]  # noqa: B018
        return True
    except NameError:
        return False


def ensure_pywebview() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        pass
    print(f"pywebview not found — installing into {sys.executable} ...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "pywebview"])
    if result.returncode != 0:
        return False
    import importlib
    importlib.invalidate_caches()
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        return False


def open_browser_when_ready(url: str) -> None:
    def poll() -> None:
        for _ in range(240):
            try:
                urllib.request.urlopen(url, timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        webbrowser.open(url)

    threading.Thread(target=poll, daemon=True, name="open-browser").start()


def ensure_node_runtime() -> None:
    """Make sure a Node.js runtime exists to run the sidecar bundle.

    Skipped when a compiled sidecar binary for this platform is already
    present (then no Node is needed).
    """
    from cline_desktop.config import (
        AppPaths,
        ensure_node_runtime as _ensure,
        find_sidecar_binary,
    )

    try:
        paths = AppPaths.discover()
    except FileNotFoundError:
        return
    if find_sidecar_binary(paths.desktop_app_dir):
        return
    _ensure()


def main() -> int:
    from cline_desktop.__main__ import main as cli

    ensure_node_runtime()

    headless = (
        running_in_ipython()
        or threading.current_thread() is not threading.main_thread()
        or not ensure_pywebview()
    )
    if headless:
        print("Running headless (no native window in this environment); "
              "the UI will open in your browser.")
        open_browser_when_ready(UI_URL)
        return cli(["--no-window"])
    return cli([])


if __name__ == "__main__":
    raise SystemExit(main())
