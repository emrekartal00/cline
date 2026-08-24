"""pywebview js_api bridge exposed to the webview as window.pywebview.api."""

from __future__ import annotations

import logging
import webbrowser
from urllib.parse import urlparse

log = logging.getLogger("cline_desktop.bridge")

_ALLOWED_SCHEMES = {"http", "https", "mailto"}


class Bridge:
    def open_external_url(self, url: str) -> bool:
        """Open a URL in the OS default browser (window.open override target)."""
        scheme = urlparse(url).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            log.warning("Refusing to open URL with scheme %r: %s", scheme, url)
            return False
        log.info("Opening external URL: %s", url)
        return webbrowser.open(url)
