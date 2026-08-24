"""Path discovery and platform helpers for the Python desktop shell."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SIDECAR_PORT = 3126
DEFAULT_UI_PORT = 3125


def find_desktop_app_dir() -> Path:
    """Locate apps/examples/desktop-app (the Tauri app this shell reuses).

    Honors CLINE_DESKTOP_APP_DIR, otherwise resolves relative to this file
    (which lives in apps/examples/desktop-app-py/src/cline_desktop/).
    """
    override = os.environ.get("CLINE_DESKTOP_APP_DIR")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"CLINE_DESKTOP_APP_DIR does not exist: {path}")
        return path

    here = Path(__file__).resolve()
    # In-monorepo layout: .../apps/examples/desktop-app-py/src/cline_desktop/config.py
    candidate = here.parents[2].parent / "desktop-app"
    if candidate.is_dir():
        return candidate
    # Standalone checkout: look for a cline clone next to this repo or in cwd.
    for base in (here.parents[2].parent, Path.cwd()):
        for repo_name in ("cline", "cline-main"):
            candidate = base / repo_name / "apps" / "examples" / "desktop-app"
            if candidate.is_dir():
                return candidate
    raise FileNotFoundError(
        "Could not locate apps/examples/desktop-app. Clone https://github.com/cline/cline "
        "next to this repo, or set CLINE_DESKTOP_APP_DIR to its apps/examples/desktop-app."
    )


def find_workspace_root(desktop_app_dir: Path) -> Path:
    """Resolve the monorepo root (needed as cwd for `bun run sidecar/index.ts`)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(desktop_app_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    # apps/examples/desktop-app -> repo root is three levels up
    return desktop_app_dir.parents[2]


def host_triple() -> str:
    """Rust-style host triple used to name the compiled sidecar binary."""
    machine = platform.machine().lower()
    arch = {
        "arm64": "aarch64",
        "aarch64": "aarch64",
        "x86_64": "x86_64",
        "amd64": "x86_64",
    }.get(machine, machine)
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if sys.platform == "win32":
        return f"{arch}-pc-windows-msvc"
    return f"{arch}-unknown-linux-gnu"


def find_bun() -> str | None:
    """Find a bun executable, checking PATH then common install locations."""
    found = shutil.which("bun")
    if found:
        return found
    candidates = [
        Path.home() / ".bun" / "bin" / "bun",
        Path("/opt/homebrew/bin/bun"),
        Path("/usr/local/bin/bun"),
    ]
    if sys.platform == "win32":
        candidates.append(Path.home() / ".bun" / "bin" / "bun.exe")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def sidecar_binary_path(desktop_app_dir: Path) -> Path:
    """Expected location/name of the compiled sidecar binary for this host."""
    suffix = ".exe" if sys.platform == "win32" else ""
    return desktop_app_dir / "src-tauri" / "bin" / f"code-sidecar-{host_triple()}{suffix}"


def find_sidecar_binary(desktop_app_dir: Path) -> Path | None:
    """Compiled sidecar binary produced by `bun run build:sidecar:bin`."""
    path = sidecar_binary_path(desktop_app_dir)
    return path if path.is_file() else None


def find_node() -> str | None:
    """Find a Node.js (>=22) executable for running the bundled sidecar."""
    found = shutil.which("node")
    if found:
        return found
    candidates = [
        Path("/opt/homebrew/bin/node"),
        Path("/usr/local/bin/node"),
        Path("/opt/homebrew/opt/node@24/bin/node"),
        Path("/opt/homebrew/opt/node@22/bin/node"),
    ]
    if sys.platform == "win32":
        for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_var)
            if base:
                candidates.append(Path(base) / "nodejs" / "node.exe")
                candidates.append(Path(base) / "Programs" / "nodejs" / "node.exe")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_sidecar_node_bundle(desktop_app_dir: Path) -> Path | None:
    """Single-file Node bundle built by `bun build --target=node` (no .exe)."""
    path = desktop_app_dir / "sidecar" / "dist-node" / "sidecar-node.mjs"
    return path if path.is_file() else None


# Release that hosts precompiled sidecar binaries too large to commit
# (GitHub caps repo files at 100 MB; the Windows binary is ~134 MB).
SIDECAR_RELEASE_URL_BASE = (
    "https://github.com/emrekartal00/cline/releases/download/sidecar-bin-v1"
)


def download_sidecar_binary(desktop_app_dir: Path) -> Path | None:
    """Fetch the platform's sidecar binary from the GitHub release.

    Returns the binary path, or None if the download failed (e.g. offline or
    no prebuilt binary published for this platform).
    """
    import logging
    import ssl
    import urllib.error
    import urllib.request

    log = logging.getLogger("cline_desktop.config")
    target = sidecar_binary_path(desktop_app_dir)
    url = f"{SIDECAR_RELEASE_URL_BASE}/{target.name}"
    tmp = target.with_suffix(target.suffix + ".download")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=30) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            copied = 0
            while chunk := resp.read(1024 * 1024):
                out.write(chunk)
                copied += len(chunk)
                if total:
                    print(
                        f"\rDownloading sidecar binary: {copied // (1024*1024)}"
                        f"/{total // (1024*1024)} MB",
                        end="",
                        flush=True,
                    )
        print()
        tmp.replace(target)
        if sys.platform != "win32":
            os.chmod(target, 0o755)
        return target
    except (urllib.error.URLError, OSError) as err:
        tmp.unlink(missing_ok=True)
        log.warning("Sidecar binary download failed: %r", err)
        log.warning("  url: %s", url)
        reason = getattr(err, "reason", None)
        if isinstance(reason, ssl.SSLError) or isinstance(err, ssl.SSLError):
            log.warning(
                "  This looks like TLS interception (common on corporate "
                "networks). Download the file in your browser instead and "
                "place it at: %s",
                target,
            )
        else:
            log.warning(
                "  If this host blocks GitHub release downloads, fetch the "
                "file in your browser and place it at: %s",
                target,
            )
        return None


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) != 0


@dataclass
class AppPaths:
    desktop_app_dir: Path
    workspace_root: Path
    webview_out: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        desktop_app_dir = find_desktop_app_dir()
        return cls(
            desktop_app_dir=desktop_app_dir,
            workspace_root=find_workspace_root(desktop_app_dir),
            webview_out=desktop_app_dir / "webview" / "out",
        )
