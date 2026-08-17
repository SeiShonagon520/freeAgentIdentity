"""Manual camoufox HAR-capture registration mode.

Opens a real camoufox browser on the ChatGPT signup page with
``record_har_path`` enabled.  The operator completes the registration by hand;
when the browser window closes the HAR file is already saved by Playwright.
The task's account record (if the flow produced one) is saved together with the
HAR for later analysis / registration-template extraction.

This mode exists because protocol (curl_cffi) requests get a Cloudflare 403
challenge even on clean IPs: Cloudflare recognises the non-browser TLS/HTTP2
fingerprint.  A real browser flow captures the exact request sequence, headers
and sentinel tokens that a successful registration uses, which the HAR
analysis tool then turns into a replayable registration template.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox


def _har_capture_dir() -> str:
    """Persisted capture directory (survives the docker image rebuild)."""
    base = os.getenv("ACCOUNT_MANAGER_CAPTURE_DIR", "")
    if not base:
        # ``data`` is mounted to /app/data in the container; fall back to cwd.
        here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..")
        base = os.path.join(os.path.abspath(here), "data", "captures")
    os.makedirs(base, exist_ok=True)
    return base


def default_capture_path(name: str = "") -> str:
    return os.path.join(
        _har_capture_dir(),
        f"{name or uuid.uuid4().hex}.har",
    )


def open_capture_browser(
    *,
    har_path: str,
    url: str = "https://chatgpt.com/auth/login",
    followup_url: str = "",
    headless: bool = False,
    proxy: str | None = None,
    timeout_seconds: int = 600,
    log: Any = None,
) -> dict[str, Any]:
    """Open camoufox on the ChatGPT signup page, recording HAR until closed.

    The default is the ChatGPT web (NextAuth) signup page.  This path registers
    an account and yields a web session without an ``add_phone`` gate.

    When ``followup_url`` is provided (e.g. a Codex OAuth authorize URL), the
    browser auto-opens it once the ChatGPT session is established (i.e. after
    the operator lands back on ``chatgpt.com``/``chatgpt.com/``), so the RT
    exchange is captured in the same HAR alongside the registration.

    Returns ``{"har_path": ..., "cancelled": bool}``.  The operator performs the
    registration manually; this function blocks until the browser is closed.
    """
    _log = log or (lambda _m, **_kw: None)
    launch_opts: dict[str, Any] = {"headless": headless}
    if proxy:
        parsed = proxy.split("://", 1)
        server = f"{parsed[0]}://{parsed[1]}" if len(parsed) == 2 else proxy
        launch_opts["proxy"] = {"server": server}
        launch_opts["geoip"] = True

    with Camoufox(**launch_opts) as browser:
        context = browser.new_context(
            record_har_path=har_path,
            record_har_url_filter="**/*",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()
        _log(f"camoufox 抓包浏览器已打开 → {url}")
        _log(f"HAR 将保存到: {har_path}")
        _log("请在浏览器中完成注册；完成后关闭浏览器窗口。")
        _followup_opened = False

        def _on_navigation(frame):
            nonlocal _followup_opened
            if not followup_url or _followup_opened:
                return
            try:
                current = str(frame.url or "")
            except Exception:
                return
            # ChatGPT web session is established only when the browser actually
            # has a session cookie for chatgpt.com AND the URL has left the auth
            # flow.  The auth pages all live under /auth/ (e.g. /auth/login,
            # /auth/logout, /auth/callback); the home page after login is
            # chatgpt.com/ (or chatgpt.com/xxx with no /auth/ and a session).
            path = urlparse(current).path.rstrip("/") or "/"
            if (
                "chatgpt.com" in current
                and not path.startswith("/auth")
                and not _followup_opened
            ):
                # Confirm a real session cookie exists (pre-auth redirects back
                # through /auth/* have none).  playwright cookies([url]) returns
                # a list of matching cookies.
                session_cookie = False
                try:
                    cookies = context.cookies("https://chatgpt.com/")
                    session_cookie = any(
                        c.get("name") == "__Secure-next-auth.session-token"
                        and str(c.get("value") or "").strip()
                        for c in cookies
                    )
                except Exception:
                    session_cookie = False
                if not session_cookie:
                    return
                _followup_opened = True
                _log("ChatGPT 会话已建立（检测到 session cookie），自动打开 Codex OAuth 授权…")
                _log(f"→ {followup_url}")
                page.goto(followup_url, wait_until="domcontentloaded", timeout=60000)

        page.on("framenavigated", _on_navigation)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            _log(f"页面打开失败: {exc}", level="warning")
        try:
            page.wait_for_event("close", timeout=max(int(timeout_seconds), 30) * 1000)
        except Exception:
            _log("等待浏览器关闭超时，正在保存 HAR...", level="warning")
        try:
            context.close()
        except Exception:
            pass
    return {"har_path": har_path, "cancelled": False}
