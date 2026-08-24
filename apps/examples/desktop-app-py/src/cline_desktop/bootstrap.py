"""JS injected into every served HTML page.

Provides the sidecar ws endpoint (priority 1 in webview/lib/desktop-client.ts:
`window.__SIDECAR_WS_ENDPOINT__`) and routes window.open to the OS browser,
because embedded webviews drop `_blank` opens when no window-opener delegate
is installed.
"""

from __future__ import annotations

import json


def build_injection(ws_endpoint: str) -> str:
    endpoint_js = json.dumps(ws_endpoint)
    return f"""<script>
window.__SIDECAR_WS_ENDPOINT__ = {endpoint_js};
(function () {{
  var nativeOpen = window.open.bind(window);
  window.open = function (url) {{
    if (url && window.pywebview && window.pywebview.api && window.pywebview.api.open_external_url) {{
      window.pywebview.api.open_external_url(String(url));
      return null;
    }}
    return nativeOpen.apply(null, arguments);
  }};
}})();
</script>"""
