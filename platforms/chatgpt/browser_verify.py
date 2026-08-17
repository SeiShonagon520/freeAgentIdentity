"""Browser-based credential verification for accounts.

curl_cffi protocol requests get flagged by Cloudflare as "not a real browser"
(HTTP 403 challenge) even on clean IPs.  Verifying an access token through a
real camoufox browser context fixes that: the ``fetch()`` runs inside a genuine
browser page, so Cloudflare sees a real browser fingerprint and origin.

This module exposes ``BrowserFetchSession`` — a small context manager that
opens one camoufox page and yields a ``browser_fetch`` callable compatible with
``check_chatgpt_access_token(..., browser_fetch=...)``.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

# BrowserFetchFn: ``(url, method, headers, body) -> {"status": int, "text": str, "headers": dict}``
BrowserFetchFn = Callable[..., dict[str, Any]]

_FETCH_JS = """
async ({ url, method, headers, body, timeoutMs }) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error(`fetch timeout after ${timeoutMs}ms`)), timeoutMs);
  try {
    const resp = await fetch(url, {
      method,
      headers: headers || {},
      body: body === null ? undefined : body,
      redirect: 'manual',
      signal: controller.signal,
    });
    const respHeaders = {};
    resp.headers.forEach((v, k) => { respHeaders[k] = v; });
    let text = '';
    try { text = await resp.text(); } catch {}
    return { ok: resp.ok, status: resp.status, url: resp.url || url, headers: respHeaders, text };
  } catch (e) {
    return { ok: false, status: 0, url, headers: {}, text: String(e && e.message || e) };
  } finally {
    clearTimeout(timer);
  }
}
"""


def make_browser_fetch(page: Any, *, timeout_ms: int = 30000) -> BrowserFetchFn:
    """Return a ``browser_fetch`` callable bound to a live browser context.

    Uses playwright's ``context.request`` (the browser network stack) instead
    of ``page.evaluate(fetch)``.  ``page.evaluate(fetch)`` is blocked by CORS
    from an ``about:blank`` origin ("NetworkError when attempting to fetch
    resource"), while ``context.request`` performs the request through the real
    browser engine with the camoufox TLS/HTTP2 fingerprint and no CORS origin.

    Playwright's page/context is not thread-safe, so concurrent calls are
    serialised on a lock (callers should still force concurrency=1 in browser
    mode for throughput).
    """
    import threading

    _lock = threading.Lock()

    def _browser_fetch(
        url: str,
        *,
        method: str = "GET",
        headers: dict | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        with _lock:
            try:
                request = page.context.request
                resp = request.get(
                    url,
                    headers=headers or {},
                    timeout=timeout_ms,
                )
                status = int(resp.status)
                resp_headers = {k: v for k, v in resp.headers.items()}
                try:
                    text = resp.text()
                except Exception:
                    text = ""
                return {"status": status, "text": text, "headers": resp_headers}
            except Exception:
                return {"status": 0, "text": "browser request failed", "headers": {}}

    return _browser_fetch


class BrowserFetchSession:
    """Open a camoufox page and provide a ``browser_fetch`` for verification.

    Usage::

        with BrowserFetchSession() as session:
            result = check_chatgpt_access_token(token, browser_fetch=session.browser_fetch)
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | None = None,
        timeout_ms: int = 30000,
    ):
        self._headless = headless
        self._proxy = proxy
        self._timeout_ms = timeout_ms
        self._manager = None
        self._browser = None
        self._page = None
        self.browser_fetch: Optional[BrowserFetchFn] = None

    def __enter__(self) -> "BrowserFetchSession":
        from camoufox.sync_api import Camoufox

        launch_opts: dict[str, Any] = {"headless": self._headless}
        if self._proxy:
            launch_opts["proxy"] = {"server": self._proxy}
        self._manager = Camoufox(**launch_opts)
        try:
            self._browser = self._manager.__enter__()
            self._page = self._browser.new_page()
            # Do NOT navigate to an external origin: Google can challenge/redirect
            # headless browsers and leave the page mid-load.  about:blank is a
            # stable, same-origin-clean context for page.evaluate(fetch).
            try:
                self._page.goto("about:blank", wait_until="load", timeout=15000)
            except Exception:
                pass
            # Wait until the JS runtime is ready before issuing any fetch.
            try:
                self._page.evaluate("1 + 1")
            except Exception:
                pass
            self.browser_fetch = make_browser_fetch(self._page, timeout_ms=self._timeout_ms)
            return self
        except Exception:
            try:
                self._manager.__exit__(None, None, None)
            finally:
                self._manager = None
                self._browser = None
                self._page = None
                self.browser_fetch = None
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._manager is not None:
                self._manager.__exit__(exc_type, exc, tb)
        except Exception:
            try:
                if self._browser is not None:
                    self._browser.close()
            except Exception:
                pass
        self._manager = None
        self._browser = None
        self._page = None
        self.browser_fetch = None
