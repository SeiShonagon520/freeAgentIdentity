from __future__ import annotations

import asyncio

from platforms.chatgpt import browser_pool, browser_register_async, browser_verify
from platforms.chatgpt.browser_register_async import (
    BrowserProxyBlockedError,
    _goto_with_retry,
)


class _FakeAsyncManager:
    instances = []

    def __init__(self, **_kwargs):
        self.kwargs = dict(_kwargs)
        self.browser = _FakeAsyncBrowser()
        self.entered = 0
        self.exited = 0
        self.__class__.instances.append(self)

    async def __aenter__(self):
        self.entered += 1
        return self.browser

    async def __aexit__(self, *_args):
        self.exited += 1


class _FakeAsyncBrowser:
    async def close(self):
        return None


def test_browser_pool_closes_the_async_camoufox_manager(monkeypatch):
    _FakeAsyncManager.instances.clear()
    monkeypatch.setattr(browser_pool, "AsyncCamoufox", _FakeAsyncManager)

    async def fake_register(_browser, **kwargs):
        return {"email": kwargs["email"]}

    monkeypatch.setattr(browser_pool, "register_in_context", fake_register)
    pool = browser_pool.BrowserProcessPool(
        headless=True,
        pool_size=2,
        max_contexts_per_browser=1,
    )
    try:
        assert pool.register(
            email="user@example.com",
            password="password",
            proxy=None,
            otp_callback=lambda: "123456",
            log_fn=lambda *_args, **_kwargs: None,
        ) == {"email": "user@example.com"}
    finally:
        pool.shutdown()

    assert len(_FakeAsyncManager.instances) == 2
    assert all(manager.entered == 1 for manager in _FakeAsyncManager.instances)
    assert all(manager.exited == 1 for manager in _FakeAsyncManager.instances)
    assert pool.capacity == 2
    assert all(
        manager.kwargs == {
            "headless": True,
            "block_images": True,
            "enable_cache": False,
        }
        for manager in _FakeAsyncManager.instances
    )


def test_browser_pool_rotates_proxy_and_opens_a_fresh_context(monkeypatch):
    _FakeAsyncManager.instances.clear()
    monkeypatch.setattr(browser_pool, "AsyncCamoufox", _FakeAsyncManager)
    proxies = []

    async def fake_register(_browser, **kwargs):
        proxies.append(kwargs["proxy"])
        if len(proxies) == 1:
            raise BrowserProxyBlockedError("VPN route blocked")
        return {"email": kwargs["email"]}

    monkeypatch.setattr(browser_pool, "register_in_context", fake_register)
    rotations = []
    pool = browser_pool.BrowserProcessPool(
        headless=True,
        pool_size=1,
        max_contexts_per_browser=1,
    )
    try:
        result = pool.register(
            email="user@example.com",
            password="password",
            proxy="http://slot-1:7901",
            proxy_rotate_callback=lambda: rotations.append(True) or "http://slot-2:7902",
            max_proxy_attempts=3,
            otp_callback=lambda: "123456",
            log_fn=lambda *_args, **_kwargs: None,
        )
    finally:
        pool.shutdown()

    assert result["email"] == "user@example.com"
    assert rotations == [True]
    assert proxies == ["http://slot-1:7901", "http://slot-2:7902"]


def test_async_registration_context_uses_lightweight_headless_options(monkeypatch):
    captured = {}

    class Context:
        async def new_page(self):
            return object()

        async def close(self):
            captured["closed"] = True

    async def fake_new_context(_browser, **kwargs):
        captured.update(kwargs)
        return Context()

    async def fake_flow(*_args, **_kwargs):
        return {"access_token": "token"}

    monkeypatch.setattr(browser_register_async, "AsyncNewContext", fake_new_context)
    monkeypatch.setattr(browser_register_async, "_browser_registration_flow", fake_flow)

    result = asyncio.run(
        browser_register_async.register_in_context(
            object(),
            email="user@example.com",
            password="password",
            proxy=None,
            otp_callback=lambda: "123456",
            log=lambda *_args, **_kwargs: None,
        )
    )

    assert result["access_token"] == "token"
    assert captured["viewport"] == {"width": 1024, "height": 720}
    assert captured["timezone_id"] == "America/New_York"
    assert captured["reduced_motion"] == "reduce"
    assert captured["service_workers"] == "block"
    assert captured["closed"] is True


def test_browser_fetch_session_exits_the_camoufox_manager(monkeypatch):
    state = {"entered": 0, "exited": 0}

    class Page:
        def goto(self, *_args, **_kwargs):
            return None

        def evaluate(self, *_args, **_kwargs):
            return 2

    class Browser:
        def new_page(self):
            return Page()

        def close(self):
            state["closed"] = True

    class Manager:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            state["entered"] += 1
            return Browser()

        def __exit__(self, *_args):
            state["exited"] += 1

    monkeypatch.setattr("camoufox.sync_api.Camoufox", Manager)
    with browser_verify.BrowserFetchSession() as session:
        assert callable(session.browser_fetch)

    assert state == {"entered": 1, "exited": 1}


def test_login_vpn_block_is_classified_for_proxy_rotation():
    class Locator:
        async def inner_text(self, **_kwargs):
            return "Unable to load site. If you are using a VPN, try turning it off."

    class Response:
        status = 403

    class Page:
        url = "https://chatgpt.com/auth/login"

        async def title(self):
            return "Unable to load site"

        def locator(self, _selector):
            return Locator()

        async def goto(self, *_args, **_kwargs):
            return Response()

    async def run():
        await _goto_with_retry(
            Page(),
            "https://chatgpt.com/auth/login",
            log=lambda *_args, **_kwargs: None,
        )

    try:
        asyncio.run(run())
    except BrowserProxyBlockedError as exc:
        assert "VPN" in str(exc) or "vpn" in str(exc).lower()
    else:
        raise AssertionError("VPN rejection must trigger proxy rotation")


def test_async_flow_never_polls_otp_twice_after_submission(monkeypatch):
    stages = iter(["password", "password", "otp", "otp", "complete"])
    otp_calls = []

    class Page:
        url = "https://auth.openai.com/email-verification"

    async def no_sleep(_seconds):
        return None

    async def fake_stage(_page):
        return next(stages)

    async def fake_selector(_page, _selectors, **_kwargs):
        return "input"

    async def fake_fill(*_args, **_kwargs):
        return True

    async def fake_click(*_args, **_kwargs):
        return "button"

    async def fake_goto(*_args, **_kwargs):
        return None

    async def fake_session(*_args, **_kwargs):
        return {"accessToken": "token"}

    async def fake_result(*_args, **_kwargs):
        return {"access_token": "token"}

    monkeypatch.setattr(browser_register_async.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(browser_register_async, "_derive_stage_from_page", fake_stage)
    monkeypatch.setattr(browser_register_async, "_wait_for_any_selector", fake_selector)
    monkeypatch.setattr(browser_register_async, "_fill_input_like_user", fake_fill)
    monkeypatch.setattr(browser_register_async, "_click_first", fake_click)
    monkeypatch.setattr(browser_register_async, "_goto_with_retry", fake_goto)
    monkeypatch.setattr(browser_register_async, "_fetch_session_via_page", fake_session)
    monkeypatch.setattr(browser_register_async, "_build_session_result", fake_result)

    result = asyncio.run(
        browser_register_async._browser_registration_flow(
            Page(),
            "user@example.com",
            "password",
            lambda: otp_calls.append(True) or "123456",
            lambda *_args, **_kwargs: None,
        )
    )

    assert result == {"access_token": "token"}
    assert otp_calls == [True]


def test_about_you_required_consent_is_checked_idempotently():
    class Checkbox:
        def __init__(self):
            self.checked = False
            self.check_calls = 0

        async def is_visible(self, **_kwargs):
            return True

        async def is_checked(self):
            return self.checked

        async def check(self, **_kwargs):
            self.check_calls += 1
            self.checked = True

    checkbox = Checkbox()

    class Checkboxes:
        async def count(self):
            return 1

        def nth(self, _index):
            return checkbox

    class Page:
        def locator(self, selector):
            assert selector == 'input[type="checkbox"]'
            return Checkboxes()

    async def run():
        page = Page()
        assert await browser_register_async._accept_about_you_consents(page, lambda *_args: None)
        assert await browser_register_async._accept_about_you_consents(page, lambda *_args: None)

    asyncio.run(run())
    assert checkbox.check_calls == 1


def test_about_you_birthday_confirmation_modal_is_accepted():
    clicked = []

    class Locator:
        @property
        def first(self):
            return self

        async def is_visible(self, **_kwargs):
            return True

        async def click(self, **_kwargs):
            clicked.append(True)

    class Page:
        def locator(self, selector):
            assert selector == '[role="dialog"] button:has-text("OK")'
            return Locator()

    result = asyncio.run(
        browser_register_async._confirm_about_you_birthday(
            Page(),
            lambda *_args: None,
            timeout=0,
        )
    )

    assert result is True
    assert clicked == [True]
