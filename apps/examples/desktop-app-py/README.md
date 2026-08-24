# Cline Desktop (Python shell)

A Python desktop app for Cline that replaces the Tauri/Rust host of
`apps/examples/desktop-app` with [pywebview](https://pywebview.flowrl.com/),
while reusing the two battle-tested pieces of the existing app **unchanged**:

- the **TypeScript sidecar backend** (`../desktop-app/sidecar`) — the process
  that embeds `@cline/core` and serves the WebSocket transport, and
- the **Next.js webview UI** (`../desktop-app/webview`).

The agent engine itself stays in TypeScript (it is ~200k LOC with no Python
port); Python owns the window, process supervision, and lifecycle — exactly
the role Rust plays in the Tauri app.

## Architecture

```
┌─────────────────────────────┐
│  Python (cline_desktop)     │
│  ┌──────────┐ ┌───────────┐ │      spawns & supervises
│  │ pywebview │ │ HTTP srv  │ │   ┌──────────────────────┐
│  │  window   │ │ :3125     │ │──▶│ sidecar (Bun/binary) │
│  └────┬─────┘ └───────────┘ │   │ ws://127.0.0.1:3126  │
└───────┼─────────────────────┘   │ /transport?approval… │
        │  loads http://127.0.0.1:3125   └──────────▲───────────┘
        ▼                                     │ WebSocket JSON
   Next.js webview (static export) ───────────┘
```

- Python spawns the sidecar (compiled `code-sidecar` binary if present,
  otherwise `bun run sidecar/index.ts`), pinning `CLINE_SIDECAR_PORT` and
  `CLINE_SIDECAR_APPROVAL_TOKEN` so crash-restarts keep the same endpoint.
- It parses the sidecar's JSON `ready` line from stdout to get the
  `wsEndpoint` (which carries the approval token).
- It serves `webview/out` on `http://127.0.0.1:3125` (a sidecar-trusted
  origin — `file://` would be rejected by the sidecar's Origin allowlist) and
  injects `window.__SIDECAR_WS_ENDPOINT__` into the HTML, which the webview's
  `desktop-client.ts` checks first when resolving its transport endpoint.
- The webview needs no Tauri APIs in this mode: every Tauri-only feature is
  behind an `isTauriAvailable()` guard that no-ops cleanly, and workspace
  picking / opening files is handled by the sidecar itself.
- `window.open` is overridden to route external links through Python's
  `webbrowser` (embedded webviews drop `_blank` opens).

## Prerequisites

- Python ≥ 3.10
- The built webview and either bun or a compiled sidecar binary:

```bash
# from the repo root
bun install

# build the webview static export (→ webview/out)
cd apps/examples/desktop-app
bun run build:ui && (cd webview && bunx next build)

# optional: compile the sidecar so end users don't need bun at runtime
bun run build:sidecar:bin        # → src-tauri/bin/code-sidecar-<host-triple>
```

Platform notes for pywebview:
- **macOS** — works out of the box (Cocoa/WKWebView).
- **Windows** — needs the WebView2 runtime (preinstalled on Win 10/11).
- **Linux** — needs GTK/WebKit2 (`PyGObject`), or install the Qt fallback:
  `pip install -e ".[qt]"` and run with `PYWEBVIEW_GUI=qt`.

## Install & run

```bash
cd apps/examples/desktop-app-py
python -m venv .venv && source .venv/bin/activate
pip install -e .

cline-desktop                 # or: python -m cline_desktop
```

Flags:

| Flag | Effect |
| --- | --- |
| `--dev` | Spawn the Next.js dev server (hot reload) instead of serving `webview/out` |
| `--dev --attach` | Use an already-running dev server on the UI port |
| `--no-window` | Headless: run sidecar + UI server, open the printed URL in a browser |
| `--debug` | Webview devtools + verbose logging |
| `--sidecar-bin PATH` | Use a specific compiled sidecar binary |
| `--sidecar-port N` / `--ui-port N` | Override the default 3126 / 3125 ports |

## Scope vs. the Tauri app

Implemented: window, sidecar lifecycle (spawn / ready handshake / crash
restart / graceful `POST /shutdown`), workspace picker (via the sidecar),
external-link opening, dev & headless modes.

Deliberately deferred (all no-op cleanly in the webview): system tray,
auto-updater, dock-icon badge, native session notifications, window-title
sync. Adding them later means defining `window.__TAURI_INTERNALS__` — note
that doing so flips `isTauriAvailable()` globally, so a shim must then cover
*all* nine Tauri commands plus the notification/window plugins, or guarded
features break instead of no-op'ing.
