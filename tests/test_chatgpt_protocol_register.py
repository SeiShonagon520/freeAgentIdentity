from __future__ import annotations

import json
from pathlib import Path
import types

import pytest
import platforms.chatgpt.protocol_register as protocol_register
from platforms.chatgpt.constants import CHATGPT_APP, OPENAI_API_ENDPOINTS, SENTINEL_REQ_URL
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.protocol_register import (
    ChatGPTProtocolRegister,
    OpenAISentinelClient,
)


class _FakeCookies:
    def get(self, key):
        return "device-from-cookie" if key == "oai-did" else None

    def get_dict(self):
        return {"oai-did": "device-from-cookie"}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.cookies = _FakeCookies()
        self.calls = []
        self.create_headers = {}
        self.create_body = {}
        self.password_body = {}
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url == f"{CHATGPT_APP}/api/auth/csrf":
            return _FakeResponse(payload={"csrfToken": "csrf-token"})
        if url == "https://auth.openai.com/authorize-start":
            return _FakeResponse(headers={"location": "/email-verification"})
        if url == f"{CHATGPT_APP}/api/auth/session":
            return _FakeResponse(
                payload={
                    "accessToken": "header.payload.signature",
                    "sessionToken": "session-token",
                    "expires": "2026-08-01T00:00:00Z",
                    "account": {"id": "account-123", "planType": "free"},
                }
            )
        return _FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?"):
            return _FakeResponse(payload={"url": "https://auth.openai.com/authorize-start"})
        if url == OPENAI_API_ENDPOINTS["validate_otp"]:
            assert kwargs["json"] == {"code": "123456"}
            return _FakeResponse(payload={"continue_url": "/create-account/password"})
        if url == SENTINEL_REQ_URL:
            request_payload = json.loads(kwargs["data"])
            return _FakeResponse(
                payload={
                    "token": "challenge-token",
                    "proofofwork": {"required": False},
                    "flow": request_payload["flow"],
                }
            )
        if url == OPENAI_API_ENDPOINTS["create_account"]:
            self.create_headers = kwargs["headers"]
            self.create_body = kwargs["json"]
            return _FakeResponse(
                payload={
                    "continue_url": f"{CHATGPT_APP}/api/auth/callback/openai?code=ok&state=test"
                }
            )
        if url == OPENAI_API_ENDPOINTS["register"]:
            self.password_body = kwargs["json"]
            return _FakeResponse(payload={"continue_url": "/about-you"})
        raise AssertionError(f"unexpected POST {url}")

    def close(self):
        self.closed = True


def test_protocol_register_completes_email_flow_without_browser():
    session = _FakeSession()
    logs = []
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        log_fn=logs.append,
    )

    result = worker.run(email="user@outlook.com", password="StrongPass123!")

    assert result["email"] == "user@outlook.com"
    assert result["password"] == "StrongPass123!"
    assert result["access_token"] == "header.payload.signature"
    assert result["session_token"] == "session-token"
    assert result["account_id"] == "account-123"
    assert session.password_body == {
        "username": "user@outlook.com",
        "password": "StrongPass123!",
    }
    assert "first_name" not in session.create_body
    assert session.closed is True
    sentinel = json.loads(session.create_headers["openai-sentinel-token"])
    assert sentinel["flow"] == "oauth_create_account"
    assert sentinel["c"] == "challenge-token"
    assert any("协议注册完成" in line for line in logs)


def test_protocol_register_adds_codex_refresh_token_to_result(monkeypatch):
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.mint_chatgpt_refresh_token_from_session",
        lambda *_args, **_kwargs: {
            "state": "valid",
            "message": "OAuth RT ready",
            "tokens": {
                "access_token": "codex-access",
                "refresh_token": "codex-refresh",
                "id_token": "codex-id",
                "client_id": "codex-client",
            },
        },
    )
    session = _FakeSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
    )

    result = worker.run(email="rt@example.com", password="StrongPass123!")

    assert result["access_token"] == "codex-access"
    assert result["refresh_token"] == "codex-refresh"
    assert result["id_token"] == "codex-id"
    assert result["client_id"] == "codex-client"


def test_protocol_register_profile_request_uses_only_supported_fields():
    session = _FakeSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
    )

    worker.run(
        email="user@example.com",
        password="StrongPass123!",
    )

    assert set(session.create_body) == {"name", "birthdate"}


def test_protocol_registration_accepts_current_chatgpt_otp_subjects():
    adapter = ChatGPTPlatform().build_protocol_mailbox_adapter()

    # Current messages are titled "Your temporary ChatGPT ... code" and may
    # not contain the old OpenAI brand keyword.
    assert adapter.otp_spec is not None
    assert adapter.otp_spec.keyword == ""


def test_protocol_registration_builds_worker_without_browser_options(monkeypatch):
    captured = {}

    class _Worker:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "platforms.chatgpt.protocol_register.ChatGPTProtocolRegister",
        _Worker,
    )
    adapter = ChatGPTPlatform().build_protocol_mailbox_adapter()
    ctx = types.SimpleNamespace(
        proxy="http://127.0.0.1:7890",
        log=lambda _message: None,
        platform=types.SimpleNamespace(is_cancel_requested=lambda: False),
    )
    artifacts = types.SimpleNamespace(otp_callback=lambda: "123456")

    adapter.worker_builder(ctx, artifacts)

    assert "sentinel_runtime" not in captured


def test_protocol_register_defaults_to_browserless_sentinel():
    worker = ChatGPTProtocolRegister(session=_FakeSession())

    assert not hasattr(worker.sentinel, "use_browser_runtime")


def test_registration_disallowed_is_policy_rejection_no_retry(monkeypatch):
    """registration_disallowed must NOT retry immediately — Phase F fix."""

    class _Session:
        def __init__(self):
            self.calls = 0

        def post(self, *_args, **_kwargs):
            self.calls += 1
            return _FakeResponse(
                status_code=403,
                payload={
                    "error": {
                        "code": "registration_disallowed",
                        "message": "rejected proof",
                    }
                },
            )

    class _Sentinel:
        def __init__(self):
            self.calls = 0

        def build_headers(self, *_args):
            self.calls += 1
            return {"openai-sentinel-token": f"proof-{self.calls}"}

    monkeypatch.setattr("platforms.chatgpt.protocol_register.time.sleep", lambda _seconds: None)
    logs = []
    worker = ChatGPTProtocolRegister(session=_Session(), log_fn=logs.append)
    worker.sentinel = _Sentinel()

    with pytest.raises(RuntimeError, match="registration_disallowed"):
        worker._create_account("Test User", "1990-01-01")

    assert worker.sentinel.calls == 1
    assert any("不立即重试" in message for message in logs)

    # But non-disallowed errors should still raise immediately too
    class _OtherSession:
        def __init__(self):
            self.calls = 0

        def post(self, *_args, **_kwargs):
            self.calls += 1
            return _FakeResponse(status_code=500, payload={})

    worker2 = ChatGPTProtocolRegister(session=_OtherSession(), log_fn=logs.append)
    worker2.sentinel = _Sentinel()

    with pytest.raises(RuntimeError, match="创建 ChatGPT 账号失败"):
        worker2._create_account("Test User", "1990-01-01")

    assert worker2.sentinel.calls == 1


def test_protocol_login_uses_the_post_otp_password_form_without_registering():
    class LoginSession:
        def __init__(self):
            self.cookies = _FakeCookies()
            self.password_form = None
            self.closed = False

        def get(self, url, **_kwargs):
            if url == f"{CHATGPT_APP}/api/auth/csrf":
                return _FakeResponse(payload={"csrfToken": "csrf-token"})
            if url == "https://auth.openai.com/authorize-start":
                return _FakeResponse(headers={"location": "/email-verification"})
            if url == "https://auth.openai.com/email-verification":
                return _FakeResponse(text="<form action=\"/email-verification\"></form>")
            if url == "https://auth.openai.com/log-in/password":
                return _FakeResponse(
                    text=(
                        '<form action="/log-in/password">'
                        '<input type="hidden" name="state" value="state-1">'
                        '<input type="password" name="password">'
                        "</form>"
                    )
                )
            if url == f"{CHATGPT_APP}/api/auth/session":
                return _FakeResponse(
                    payload={
                        "accessToken": "header.payload.signature",
                        "account": {"id": "account-123"},
                    }
                )
            return _FakeResponse()

        def post(self, url, **kwargs):
            if url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?"):
                return _FakeResponse(payload={"url": "https://auth.openai.com/authorize-start"})
            if url == OPENAI_API_ENDPOINTS["validate_otp"]:
                assert kwargs["json"] == {"code": "123456"}
                return _FakeResponse(payload={"continue_url": "/log-in/password"})
            if url == "https://auth.openai.com/log-in/password":
                self.password_form = kwargs["data"]
                return _FakeResponse(status_code=302, headers={"location": "https://chatgpt.com/"})
            raise AssertionError(f"unexpected POST {url}")

        def close(self):
            self.closed = True

    session = LoginSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
    )

    result = worker.login(email="user@example.com", password="StrongPass123!")

    assert result["access_token"] == "header.payload.signature"
    assert session.password_form == "state=state-1&password=StrongPass123%21"
    assert session.closed is True


def test_sentinel_headers_include_v8_and_session_observer_tokens(monkeypatch):
    captured = {}

    class _FakePool:
        def execute(self, **payload):
            captured.update(payload)
            return {
                "p": "v8-enforcement-proof",
                "t": "turnstile-proof",
                "so": "observer-proof",
            }

    class _Session:
        def post(self, *_args, **_kwargs):
            return _FakeResponse(
                payload={
                    "token": "challenge",
                    "proofofwork": {"required": False},
                    "turnstile": {"required": True, "dx": "turnstile-dx"},
                    "so": {"required": True, "collector_dx": "observer-dx"},
                }
            )

    monkeypatch.setattr(
        protocol_register,
        "get_sentinel_sdk",
        lambda _session: types.SimpleNamespace(
            path=Path("sentinel-sdk.js"),
            version="test-version",
            url="https://sentinel.example/sentinel/test-version/sdk.js",
        ),
    )
    monkeypatch.setattr(protocol_register, "get_sentinel_vm_pool", lambda: _FakePool())
    client = OpenAISentinelClient(session=_Session(), user_agent="test-agent")
    headers = client.build_headers("device-1", "oauth_create_account")
    assert set(headers) == {
        "openai-sentinel-token",
        "openai-sentinel-so-token",
    }
    token = json.loads(headers["openai-sentinel-token"])
    so_token = json.loads(headers["openai-sentinel-so-token"])
    assert token["p"] == "v8-enforcement-proof"
    assert token["t"] == "turnstile-proof"
    assert so_token["so"] == "observer-proof"
    assert captured["challenge"]["_python_proof"]
    assert captured["challenge"]["turnstile"]["dx"] == "turnstile-dx"
    assert captured["sdk"].endswith("sentinel-sdk.js")
