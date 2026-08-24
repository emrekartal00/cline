#!/usr/bin/env python3
"""One-click launcher for the Cline desktop app (Python shell).

Download this repo (ZIP is fine — no git needed) and run this file with
Python 3.10+. It delegates to apps/examples/desktop-app-py/run.py, which
installs pywebview into the running interpreter if missing and starts the
app. On Apple Silicon macOS the bundled sidecar binary and prebuilt webview
mean no other tooling is required; on other platforms see
apps/examples/desktop-app-py/README.md for the one-time build steps.
"""

import runpy
from pathlib import Path

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    runpy.run_path(str(HERE / "apps" / "examples" / "desktop-app-py" / "run.py"), run_name="__main__")
