#!/usr/bin/env python3
"""Enterprise launcher for the Cline Python desktop app.

Run this file (Spyder: open it and press Run; terminal: `python run_cline.py`)
to start Cline with fixed enterprise settings — no command-line flags needed.

Prerequisite (one-time, so nothing is fetched at runtime): install a Node.js
runtime into the SAME Python you launch with:

    pip install nodejs-wheel-binaries

(Offline: download that wheel elsewhere and `pip install path\\to\\wheel.whl`.)

Then just run this file every time. With CLINE_DESKTOP_NO_DOWNLOAD=1 the app
never downloads or pip-installs anything at runtime; if something is missing
it fails with a clear "pre-provision this" message instead of reaching out.
"""

import os
import sys

# --- Enterprise settings (edit here once; no flags to pass) -----------------
# Block ALL runtime downloads / pip-installs (sidecar binary, Node, pywebview).
os.environ.setdefault("CLINE_DESKTOP_NO_DOWNLOAD", "1")

# Flags passed to the app on every launch:
#   --no-window     run headless and open the UI in your browser
#   --insecure-tls  accept self-signed / internal-CA certificates
#   --omit-model    strip the `model` field (single-model GGUF endpoints)
# Remove any you don't want.
APP_FLAGS = ["--no-window", "--insecure-tls", "--omit-model"]
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (
    os.path.join(HERE, "apps", "examples", "desktop-app-py", "src"),  # monorepo/fork layout
    os.path.join(HERE, "src"),                                        # standalone repo layout
):
    if os.path.isdir(os.path.join(_candidate, "cline_desktop")):
        sys.path.insert(0, _candidate)
        break

from cline_desktop.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(APP_FLAGS))
