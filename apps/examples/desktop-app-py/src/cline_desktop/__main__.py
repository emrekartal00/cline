"""CLI entry point: `python -m cline_desktop` or `cline-desktop`."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .app import App, Options


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cline-desktop",
        description="Cline desktop app (Python shell reusing the TS sidecar + webview).",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Run against the Next.js dev server instead of the built webview/out",
    )
    parser.add_argument(
        "--attach",
        action="store_true",
        help="With --dev: attach to an already-running dev server instead of spawning one",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="Run sidecar + UI server without opening a window (use a browser)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable webview devtools + verbose logs")
    parser.add_argument(
        "--insecure-tls",
        action="store_true",
        help="Disable TLS certificate verification for API calls (self-signed / internal-CA enterprise endpoints)",
    )
    parser.add_argument("--sidecar-bin", type=Path, help="Path to a compiled code-sidecar binary")
    parser.add_argument("--sidecar-port", type=int, help="Preferred sidecar port (default 3126)")
    parser.add_argument("--ui-port", type=int, help="Preferred UI port (default 3125)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    options = Options(
        dev=args.dev,
        attach=args.attach,
        no_window=args.no_window,
        debug=args.debug,
        insecure_tls=args.insecure_tls,
        sidecar_bin=args.sidecar_bin,
        sidecar_port=args.sidecar_port,
        ui_port=args.ui_port,
    )
    return App(options).run()


if __name__ == "__main__":
    raise SystemExit(main())
