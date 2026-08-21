from __future__ import annotations

import base64
import json
from pathlib import Path
import types
from urllib.parse import parse_qs, urlparse

import pytest
from curl_cffi import CurlECode, CurlError
import platforms.chatgpt.protocol_register as protocol_register
from platforms.chatgpt.constants import (
    CHATGPT_APP,
    CODEX_CLIENT_ID,
    OPENAI_API_ENDPOINTS,
    OPENAI_AUTH,
    OAUTH_TOKEN_URL,
    SENTINEL_REQ_URL,
)
from platforms.chatgpt.environment_profile import (
    FingerprintPool,
    PROTOCOL_CHROME_IMPERSONATE,
    PROTOCOL_CHROME_VERSION,
)
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
    def __init__(self, status_code=200, payload=None, *, headers=None, text="", url=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text
        self.url = url

    def json(self):
        return self._payload


def _fake_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{header}.{body}.signature"


class _FakeSession:
    def __init__(self):
        self.cookies = _FakeCookies()
        self.calls = []
        self.create_headers = {}
        self.create_body = {}
        self.password_body = {}
        self.signup_body = {}
        self.signup_headers = {}
        self.oauth_state = ""
        self.current_email = ""
        self.send_otp_calls = 0
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.startswith(f"{OPENAI_AUTH}/oauth/authorize?"):
            self.oauth_state = str(
                (parse_qs(urlparse(url).query).get("state") or [""])[0]
            )
            return _FakeResponse(url=f"{OPENAI_AUTH}/create-account")
        if url == OPENAI_API_ENDPOINTS["send_otp"]:
            self.send_otp_calls += 1
            return _FakeResponse(payload={"ok": True}, url=url)
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
        if url == OPENAI_API_ENDPOINTS["signup"]:
            self.signup_body = kwargs["json"]
            self.signup_headers = kwargs["headers"]
            self.current_email = str(kwargs["json"]["username"]["value"])
            return _FakeResponse(
                payload={
                    "continue_url": "/create-account/password",
                    "page": {"type": "create_account_password"},
                }
            )
        if url == OPENAI_API_ENDPOINTS["validate_otp"]:
            assert kwargs["json"] == {"code": "123456"}
            return _FakeResponse(
                payload={"continue_url": "/about-you", "page": {"type": "about_you"}}
            )
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
                    "continue_url": (
                        "http://localhost:1455/auth/callback?code=ok"
                        f"&state={self.oauth_state}"
                    )
                }
            )
        if url == OPENAI_API_ENDPOINTS["register"]:
            self.password_body = kwargs["json"]
            return _FakeResponse(
                payload={
                    "continue_url": "/email-verification",
                    "page": {"type": "email_otp_verification"},
                }
            )
        if url == OAUTH_TOKEN_URL:
            assert kwargs["data"]["client_id"] == CODEX_CLIENT_ID
            return _FakeResponse(
                payload={
                    "access_token": "codex-access",
                    "refresh_token": "codex-refresh",
                    "id_token": _fake_jwt(
                        {
                            "email": self.current_email,
                            "https://api.openai.com/auth": {
                                "chatgpt_account_id": "account-123",
                                "organization_id": "workspace-123",
                            },
                        }
                    ),
                    "expires_in": 3600,
                }
            )
        raise AssertionError(f"unexpected POST {url}")

    def close(self):
        self.closed = True


def _install_valid_web_session_mint(monkeypatch):
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.mint_chatgpt_refresh_token_from_session",
        lambda *_args, **_kwargs: {
            "state": "valid",
            "message": "web session exchanged",
            "tokens": {
                "access_token": "web-codex-access",
                "refresh_token": "web-codex-refresh",
                "id_token": "web-codex-id",
                "client_id": CODEX_CLIENT_ID,
            },
        },
    )


@pytest.fixture(autouse=True)
def _install_valid_protocol_totp(monkeypatch):
    monkeypatch.setattr(
        "platforms.chatgpt.mfa.bind_totp_2fa",
        lambda *_args, **_kwargs: {
            "activated": True,
            "secret": "TESTPROTOCOLTOTP",
            "result": {"success": True},
        },
    )


def test_protocol_register_completes_email_flow_without_browser(monkeypatch):
    _install_valid_web_session_mint(monkeypatch)
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
    assert result["access_token"] == "web-codex-access"
    assert result["refresh_token"] == "web-codex-refresh"
    assert result["client_id"] == CODEX_CLIENT_ID
    assert result["session_token"] == "session-token"
    assert result["account_id"] == "account-123"
    assert result["workspace_id"] == "account-123"
    assert result["password_registered"] is True
    assert result["totp_2fa"] == {
        "requested": True,
        "bound": True,
        "secret": "TESTPROTOCOLTOTP",
        "error": "",
    }
    assert session.password_body == {
        "username": "user@outlook.com",
        "password": "StrongPass123!",
    }
    assert session.signup_body == {
        "username": {"value": "user@outlook.com", "kind": "email"},
        "screen_hint": "signup",
    }
    signup_sentinel = json.loads(session.signup_headers["openai-sentinel-token"])
    assert signup_sentinel["flow"] == "authorize_continue"
    assert any(url == CHATGPT_APP for method, url, _kwargs in session.calls if method == "GET")
    assert session.send_otp_calls == 0
    register_index = next(
        index for index, (method, url, _kwargs) in enumerate(session.calls)
        if method == "POST" and url == OPENAI_API_ENDPOINTS["register"]
    )
    validate_index = next(
        index for index, (method, url, _kwargs) in enumerate(session.calls)
        if method == "POST" and url == OPENAI_API_ENDPOINTS["validate_otp"]
    )
    create_index = next(
        index for index, (method, url, _kwargs) in enumerate(session.calls)
        if method == "POST" and url == OPENAI_API_ENDPOINTS["create_account"]
    )
    assert register_index < validate_index < create_index
    assert "first_name" not in session.create_body
    assert session.closed is True
    sentinel = json.loads(session.create_headers["openai-sentinel-token"])
    assert sentinel["flow"] == "oauth_create_account"
    assert sentinel["c"] == "challenge-token"
    assert any("ChatGPT Web 协议注册" in line for line in logs)


def test_protocol_register_accepts_otp_first_authorization_state(monkeypatch):
    _install_valid_web_session_mint(monkeypatch)
    class OtpFirstSession(_FakeSession):
        def post(self, url, **kwargs):
            if url == OPENAI_API_ENDPOINTS["signup"]:
                self.calls.append(("POST", url, kwargs))
                self.signup_body = kwargs["json"]
                self.signup_headers = kwargs["headers"]
                self.current_email = str(kwargs["json"]["username"]["value"])
                return _FakeResponse(
                    payload={
                        "continue_url": "/email-verification",
                        "page": {"type": "email_otp_verification"},
                    }
                )
            if url == OPENAI_API_ENDPOINTS["validate_otp"]:
                self.calls.append(("POST", url, kwargs))
                return _FakeResponse(
                    payload={
                        "continue_url": "/about-you",
                        "page": {"type": "about_you"},
                    }
                )
            if url == OPENAI_API_ENDPOINTS["register"]:
                self.calls.append(("POST", url, kwargs))
                self.password_body = kwargs["json"]
                return _FakeResponse(
                    payload={
                        "continue_url": "/email-verification",
                        "page": {"type": "email_otp_verification"},
                    }
                )
            return super().post(url, **kwargs)

    session = OtpFirstSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
    )

    result = worker.run(email="otp-first@example.com", password="StrongPass123!")

    assert result["refresh_token"] == "web-codex-refresh"
    validate_index = next(
        index for index, (method, url, _kwargs) in enumerate(session.calls)
        if method == "POST" and url == OPENAI_API_ENDPOINTS["validate_otp"]
    )
    register_index = next(
        index for index, (method, url, _kwargs) in enumerate(session.calls)
        if method == "POST" and url == OPENAI_API_ENDPOINTS["register"]
    )
    assert register_index < validate_index
    assert result["password_registered"] is True


def test_protocol_register_rejects_otp_first_flow_when_password_creation_fails(monkeypatch):
    _install_valid_web_session_mint(monkeypatch)

    class PasswordRejectedSession(_FakeSession):
        def post(self, url, **kwargs):
            if url == OPENAI_API_ENDPOINTS["signup"]:
                self.calls.append(("POST", url, kwargs))
                self.current_email = str(kwargs["json"]["username"]["value"])
                return _FakeResponse(
                    payload={
                        "continue_url": "/email-verification",
                        "page": {"type": "email_otp_verification"},
                    }
                )
            if url == OPENAI_API_ENDPOINTS["register"]:
                self.calls.append(("POST", url, kwargs))
                return _FakeResponse(
                    status_code=400,
                    payload={"error": {"message": "password rejected"}},
                )
            return super().post(url, **kwargs)

    session = PasswordRejectedSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
    )

    with pytest.raises(RuntimeError, match="设置 ChatGPT 密码失败"):
        worker.run(email="password-required@example.com", password="StrongPass123!")

    assert not any(
        method == "POST" and url == OPENAI_API_ENDPOINTS["validate_otp"]
        for method, url, _kwargs in session.calls
    )


def test_protocol_register_fails_when_same_session_totp_binding_is_rejected(monkeypatch):
    _install_valid_web_session_mint(monkeypatch)
    monkeypatch.setattr(
        "platforms.chatgpt.mfa.bind_totp_2fa",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 403")),
    )
    session = _FakeSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
    )

    with pytest.raises(RuntimeError, match="协议注册 TOTP 2FA 绑定失败"):
        worker.run(email="totp-required@example.com", password="StrongPass123!")

    assert session.closed is True


def test_protocol_register_lets_create_account_decide_after_add_phone_hint():
    class AddPhoneHintSession(_FakeSession):
        def post(self, url, **kwargs):
            if url == OPENAI_API_ENDPOINTS["validate_otp"]:
                self.calls.append(("POST", url, kwargs))
                return _FakeResponse(
                    payload={
                        "continue_url": "/add-phone",
                        "page": {"type": "add_phone"},
                    }
                )
            return super().post(url, **kwargs)

    session = AddPhoneHintSession()
    logs = []
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        log_fn=logs.append,
    )

    result = worker._run_codex_registration(
        email="phone-hint@example.com",
        password="StrongPass123!",
    )

    assert result["access_token"] == "codex-access"
    assert result["refresh_token"] == "codex-refresh"
    assert result["password_registered"] is True
    assert any(
        method == "POST" and url == OPENAI_API_ENDPOINTS["create_account"]
        for method, url, _kwargs in session.calls
    )
    assert any("create_account" in message for message in logs)


def test_protocol_register_uses_web_registration_before_codex_token_mint(monkeypatch):
    class DirectBootstrapFailureSession(_FakeSession):
        def get(self, url, **kwargs):
            if url.startswith(f"{OPENAI_AUTH}/oauth/authorize?"):
                self.calls.append(("GET", url, kwargs))
                return _FakeResponse(status_code=500, text="direct oauth unavailable", url=url)
            return super().get(url, **kwargs)

    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.mint_chatgpt_refresh_token_from_session",
        lambda *_args, **_kwargs: {
            "state": "valid",
            "message": "legacy web session exchanged",
            "tokens": {
                "access_token": "legacy-access",
                "refresh_token": "legacy-refresh",
                "id_token": "legacy-id",
                "client_id": CODEX_CLIENT_ID,
            },
        },
    )
    session = DirectBootstrapFailureSession()
    logs = []
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
        log_fn=logs.append,
    )

    result = worker.run(email="fallback@example.com", password="StrongPass123!")

    assert result["refresh_token"] == "legacy-refresh"
    assert any(
        method == "POST" and url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?")
        for method, url, _kwargs in session.calls
    )
    assert not any(
        method == "GET" and url.startswith(f"{OPENAI_AUTH}/oauth/authorize?")
        for method, url, _kwargs in session.calls
    )
    assert any("ChatGPT Web 协议注册" in message for message in logs)


def test_protocol_register_reports_oauth_error_redirect_without_waiting_for_otp():
    encoded_error = base64.urlsafe_b64encode(
        json.dumps({"errorCode": "rate_limit_exceeded"}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    error_url = f"https://auth.openai.com/error?payload={encoded_error}"

    class ErrorSession:
        cookies = _FakeCookies()

        @staticmethod
        def get(url, **_kwargs):
            if url == f"{CHATGPT_APP}/api/auth/csrf":
                return _FakeResponse(payload={"csrfToken": "csrf-token"}, url=url)
            if url == "https://auth.openai.com/authorize-start":
                return _FakeResponse(
                    status_code=302,
                    headers={"location": error_url},
                    url=url,
                )
            if url == error_url:
                return _FakeResponse(url=error_url, text="OpenAI error")
            return _FakeResponse(url=url)

        @staticmethod
        def post(url, **_kwargs):
            if url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?"):
                return _FakeResponse(
                    payload={"url": "https://auth.openai.com/authorize-start"},
                    url=url,
                )
            raise AssertionError(f"unexpected POST {url}")

    worker = ChatGPTProtocolRegister(session=ErrorSession())

    with pytest.raises(RuntimeError, match="rate_limit_exceeded"):
        worker._initialize_signup("user@example.com")


def test_protocol_register_adds_codex_refresh_token_to_result(monkeypatch):
    _install_valid_web_session_mint(monkeypatch)
    session = _FakeSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: "123456",
    )

    result = worker.run(email="rt@example.com", password="StrongPass123!")

    assert result["access_token"] == "web-codex-access"
    assert result["refresh_token"] == "web-codex-refresh"
    assert result["id_token"]
    assert result["client_id"] == CODEX_CLIENT_ID


def test_protocol_register_treats_add_phone_as_normal_no_rt(monkeypatch):
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.mint_chatgpt_refresh_token_from_session",
        lambda *_args, **_kwargs: {
            "state": "unknown",
            "message": "Codex OAuth 要求手机号验证，本次 RT 获取失败（未执行短信接码）",
            "tokens": {},
        },
    )
    logs = []
    worker = ChatGPTProtocolRegister(
        session=_FakeSession(),
        otp_callback=lambda: "123456",
        log_fn=logs.append,
    )

    result = worker.run(email="phone-gated@example.com", password="StrongPass123!")

    assert result["access_token"] == "header.payload.signature"
    assert result["refresh_token"] == ""
    assert result["id_token"] == ""
    assert result["client_id"] == ""
    assert any("命中 add_phone，按正常无 RT 账号保存" in message for message in logs)
    assert any("正常无 RT 状态" in message for message in logs)


def test_protocol_register_profile_request_uses_only_supported_fields(monkeypatch):
    _install_valid_web_session_mint(monkeypatch)
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


def test_protocol_profiles_use_current_supported_chrome_fingerprint():
    profiles = FingerprintPool.from_us_en_desktop().profiles

    assert len(profiles) == 7
    assert profiles[0].impersonate == "firefox144"
    assert {profile.impersonate for profile in profiles[1:]} == {
        PROTOCOL_CHROME_IMPERSONATE
    }
    assert all(
        f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0" in profile.user_agent
        for profile in profiles[1:]
    )


def test_protocol_register_default_session_uses_current_chrome_fingerprint(monkeypatch):
    captured = {}

    def create_session(**kwargs):
        captured.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(protocol_register.requests, "Session", create_session)

    worker = ChatGPTProtocolRegister()

    assert captured["impersonate"] == PROTOCOL_CHROME_IMPERSONATE
    assert f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0" in worker.user_agent


def test_protocol_register_rotates_proxy_and_rebuilds_session_on_cloudflare(monkeypatch):
    class ChallengeSession(_FakeSession):
        def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            if url == CHATGPT_APP:
                return _FakeResponse(
                    status_code=403,
                    headers={"content-type": "text/html; charset=utf-8"},
                    text="<html><title>Just a moment...</title></html>",
                    url=url,
                )
            return super().get(url, **kwargs)

    sessions = [ChallengeSession(), _FakeSession()]
    created = []

    def create_session(**_kwargs):
        session = sessions.pop(0)
        created.append(session)
        return session

    monkeypatch.setattr(protocol_register.requests, "Session", create_session)
    _install_valid_web_session_mint(monkeypatch)
    rotations = []
    worker = ChatGPTProtocolRegister(
        proxy="http://mihomo:7901",
        otp_callback=lambda: "123456",
        proxy_rotate_callback=lambda: rotations.append(True) or "http://mihomo:7901",
    )

    result = worker.run(email="rotate@example.com", password="StrongPass123!")

    assert result["access_token"] == "web-codex-access"
    assert rotations == [True]
    assert len(created) == 2
    assert all(session.closed for session in created)


def test_protocol_register_rotates_proxy_when_signup_submission_is_challenged(monkeypatch):
    class SignupChallengeSession(_FakeSession):
        def post(self, url, **kwargs):
            if url == OPENAI_API_ENDPOINTS["signup"]:
                self.calls.append(("POST", url, kwargs))
                return _FakeResponse(
                    status_code=403,
                    headers={"content-type": "text/html; charset=utf-8"},
                    text="<html><title>Just a moment...</title></html>",
                    url=url,
                )
            return super().post(url, **kwargs)

    sessions = [SignupChallengeSession(), _FakeSession()]
    created = []

    def create_session(**_kwargs):
        session = sessions.pop(0)
        created.append(session)
        return session

    monkeypatch.setattr(protocol_register.requests, "Session", create_session)
    rotations = []
    worker = ChatGPTProtocolRegister(
        proxy="http://mihomo:7901",
        otp_callback=lambda: "123456",
        proxy_rotate_callback=lambda: rotations.append(True) or "http://mihomo:7902",
    )

    result = worker._run_codex_registration(
        email="signup-cf@example.com",
        password="StrongPass123!",
    )

    assert result["refresh_token"] == "codex-refresh"
    assert rotations == [True]
    assert len(created) == 2
    assert created[0].closed is True
    assert created[1].closed is False


def test_non_cloudflare_html_403_is_not_treated_as_a_challenge():
    response = _FakeResponse(
        status_code=403,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<html><title>Access denied</title></html>",
    )

    assert protocol_register._is_cloudflare_challenge_response(response) is False


def test_normal_oauth_html_with_cloudflare_script_text_is_not_a_challenge():
    response = _FakeResponse(
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8", "server": "cloudflare"},
        text=(
            "<html><script>window.cloudflare = true</script>"
            "<script src=\"/cdn-cgi/challenge-platform/scripts/jsd/main.js\"></script>"
            "<body>OAuth authorize</body></html>"
        ),
    )

    assert protocol_register._is_cloudflare_challenge_response(response) is False


def test_cloudflare_challenge_page_with_http_200_is_detected():
    response = _FakeResponse(
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<html><title>Just a moment...</title></html>",
    )

    assert protocol_register._is_cloudflare_challenge_response(response) is True


def test_oauth_initialization_retries_transient_tls_error_with_new_session(monkeypatch):
    class _TlsFailureSession(_FakeSession):
        def get(self, url, **kwargs):
            if url.startswith(f"{OPENAI_AUTH}/oauth/authorize?"):
                raise CurlError(
                    "TLS connect error",
                    CurlECode.SSL_CONNECT_ERROR,
                )
            return super().get(url, **kwargs)

    first = _TlsFailureSession()
    second = _FakeSession()
    sessions = [first, second]
    created = []

    def create_session(**_kwargs):
        session = sessions.pop(0)
        created.append(session)
        return session

    monkeypatch.setattr(protocol_register.requests, "Session", create_session)
    logs = []
    worker = ChatGPTProtocolRegister(log_fn=logs.append)
    waits = []
    monkeypatch.setattr(worker, "_wait_before_oauth_retry", waits.append)

    response = worker._initialize_codex_registration("user@example.com")

    assert response is not None
    assert created == [first, second]
    assert first.closed is True
    assert len(waits) == 1
    assert any("curl(35)" in message for message in logs)


def test_oauth_initialization_does_not_retry_certificate_failure(monkeypatch):
    class _CertificateFailureSession(_FakeSession):
        def get(self, url, **kwargs):
            if url.startswith(f"{OPENAI_AUTH}/oauth/authorize?"):
                raise CurlError(
                    "certificate verify failed",
                    CurlECode.PEER_FAILED_VERIFICATION,
                )
            return super().get(url, **kwargs)

    session = _CertificateFailureSession()
    created = []

    def create_session(**_kwargs):
        created.append(session)
        return session

    monkeypatch.setattr(protocol_register.requests, "Session", create_session)
    worker = ChatGPTProtocolRegister()

    with pytest.raises(CurlError, match="certificate verify failed"):
        worker._initialize_codex_registration("user@example.com")

    assert created == [session]
    assert session.closed is False


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


def test_protocol_login_uses_the_post_otp_password_form_without_registering(monkeypatch):
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.mint_chatgpt_refresh_token_from_session",
        lambda *_args, **_kwargs: {
            "state": "unknown",
            "message": "not available in this login fixture",
            "tokens": {},
        },
    )
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


def test_protocol_login_uses_password_then_saved_totp_without_reading_mailbox(monkeypatch):
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.mint_chatgpt_refresh_token_from_session",
        lambda *_args, **_kwargs: {
            "state": "unknown",
            "message": "not available in this login fixture",
            "tokens": {},
        },
    )
    monkeypatch.setattr(
        "platforms.chatgpt.mfa.totp_code",
        lambda secret: "654321" if secret == "SAVEDTOTPSECRET" else "",
    )

    class TotpLoginSession:
        def __init__(self):
            self.cookies = _FakeCookies()
            self.password_form = None
            self.totp_form = None
            self.signin_url = ""
            self.closed = False

        def get(self, url, **_kwargs):
            if url == CHATGPT_APP:
                return _FakeResponse(url=url)
            if url == f"{CHATGPT_APP}/api/auth/csrf":
                return _FakeResponse(payload={"csrfToken": "csrf-token"}, url=url)
            if url == "https://auth.openai.com/authorize-start":
                return _FakeResponse(
                    status_code=302,
                    headers={"location": "/email-verification"},
                    url=url,
                )
            if url == "https://auth.openai.com/email-verification":
                return _FakeResponse(
                    text='<form action="/email-verification"></form>',
                    url=url,
                )
            if url == "https://auth.openai.com/log-in/password":
                return _FakeResponse(
                    text=(
                        '<form action="/log-in/password">'
                        '<input type="hidden" name="state" value="password-state">'
                        '<input type="password" name="password">'
                        "</form>"
                    ),
                    url=url,
                )
            if url == "https://auth.openai.com/mfa":
                return _FakeResponse(
                    text=(
                        '<h1>Enter the code from your authenticator app</h1>'
                        '<form action="/mfa">'
                        '<input type="hidden" name="state" value="mfa-state">'
                        '<input autocomplete="one-time-code" name="totp">'
                        "</form>"
                    ),
                    url=url,
                )
            if url == f"{CHATGPT_APP}/api/auth/session":
                return _FakeResponse(
                    payload={
                        "accessToken": "header.payload.signature",
                        "account": {"id": "account-123"},
                    },
                    url=url,
                )
            return _FakeResponse(url=url)

        def post(self, url, **kwargs):
            if url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?"):
                self.signin_url = url
                return _FakeResponse(
                    payload={"url": "https://auth.openai.com/authorize-start"},
                    url=url,
                )
            if url == "https://auth.openai.com/log-in/password":
                self.password_form = kwargs["data"]
                return _FakeResponse(
                    status_code=302,
                    headers={"location": "/mfa"},
                    url=url,
                )
            if url == "https://auth.openai.com/mfa":
                self.totp_form = kwargs["data"]
                return _FakeResponse(
                    status_code=302,
                    headers={"location": f"{CHATGPT_APP}/"},
                    url=url,
                )
            raise AssertionError(f"unexpected POST {url}")

        def close(self):
            self.closed = True

    mailbox_reads = []
    session = TotpLoginSession()
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=lambda: mailbox_reads.append(True) or "123456",
        totp_secret="SAVEDTOTPSECRET",
    )

    result = worker.login(email="user@example.com", password="StrongPass123!")

    assert result["access_token"] == "header.payload.signature"
    assert session.password_form == "state=password-state&password=StrongPass123%21"
    assert session.totp_form == "state=mfa-state&totp=654321"
    assert "screen_hint=login" in session.signin_url
    assert mailbox_reads == []
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
