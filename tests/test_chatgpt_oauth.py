from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest

from platforms.chatgpt.constants import (
    CODEX_CLIENT_ID,
    CODEX_REDIRECT_URI,
    CODEX_SCOPE,
    OAUTH_TOKEN_URL,
)
from platforms.chatgpt.oauth import generate_oauth_url, submit_callback_url


def _jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_codex_oauth_url_uses_pkce_and_simplified_flow_parameters():
    oauth = generate_oauth_url(
        redirect_uri=CODEX_REDIRECT_URI,
        scope=CODEX_SCOPE,
        client_id=CODEX_CLIENT_ID,
    )
    parsed = urlparse(oauth.auth_url)
    params = parse_qs(parsed.query)
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(oauth.code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")

    assert parsed.path == "/oauth/authorize"
    assert params["client_id"] == [CODEX_CLIENT_ID]
    assert params["redirect_uri"] == [CODEX_REDIRECT_URI]
    assert params["scope"] == [CODEX_SCOPE]
    assert params["state"] == [oauth.state]
    assert params["code_challenge"] == [expected_challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["id_token_add_organizations"] == ["true"]
    assert params["codex_cli_simplified_flow"] == ["true"]


def test_submit_callback_validates_state_and_exchanges_codex_tokens():
    calls = []
    id_token = _jwt({
        "email": "oauth@example.com",
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-oauth"},
    })

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "access_token": "oauth-access",
                "refresh_token": "oauth-refresh",
                "id_token": id_token,
                "expires_in": 3600,
            }

    class Session:
        @staticmethod
        def post(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    result = json.loads(submit_callback_url(
        callback_url=f"{CODEX_REDIRECT_URI}?code=auth-code&state=expected-state",
        expected_state="expected-state",
        code_verifier="pkce-verifier",
        redirect_uri=CODEX_REDIRECT_URI,
        client_id=CODEX_CLIENT_ID,
        session=Session(),
    ))

    assert result["access_token"] == "oauth-access"
    assert result["refresh_token"] == "oauth-refresh"
    assert result["id_token"] == id_token
    assert result["account_id"] == "acct-oauth"
    assert result["email"] == "oauth@example.com"
    assert calls[0][0] == OAUTH_TOKEN_URL
    assert calls[0][1]["data"] == {
        "grant_type": "authorization_code",
        "client_id": CODEX_CLIENT_ID,
        "code": "auth-code",
        "redirect_uri": CODEX_REDIRECT_URI,
        "code_verifier": "pkce-verifier",
    }

    with pytest.raises(ValueError, match="state mismatch"):
        submit_callback_url(
            callback_url=f"{CODEX_REDIRECT_URI}?code=auth-code&state=wrong-state",
            expected_state="expected-state",
            code_verifier="pkce-verifier",
            session=Session(),
        )


def test_web_session_mints_refresh_token_through_codex_oauth():
    from platforms.chatgpt.credential_checks import mint_chatgpt_refresh_token_from_session

    class Cookies:
        def __init__(self):
            self.values = {}

        def set(self, name, value):
            self.values[name] = value

    class Response:
        def __init__(self, *, status_code=200, headers=None, payload=None):
            self.status_code = status_code
            self.headers = headers or {}
            self._payload = payload or {}
            self.text = ""

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.cookies = Cookies()
            self.auth_url = ""

        def get(self, url, **_kwargs):
            self.auth_url = url
            params = parse_qs(urlparse(url).query)
            callback = f"{CODEX_REDIRECT_URI}?code=session-code&state={params['state'][0]}"
            return Response(status_code=302, headers={"location": callback})

        @staticmethod
        def post(url, **_kwargs):
            assert url == OAUTH_TOKEN_URL
            return Response(payload={
                "access_token": "session-access",
                "refresh_token": "session-refresh",
                "id_token": "session-id",
            })

    session = Session()
    result = mint_chatgpt_refresh_token_from_session(
        "session-token=active",
        session=session,
    )

    assert result["state"] == "valid"
    assert result["tokens"]["refresh_token"] == "session-refresh"
    assert urlparse(session.auth_url).path == "/oauth/authorize"
    assert parse_qs(urlparse(session.auth_url).query)["prompt"] == ["none"]
    assert session.cookies.values == {}


def test_web_session_oauth_retries_a_transient_html_200(monkeypatch):
    from platforms.chatgpt.credential_checks import mint_chatgpt_refresh_token_from_session

    class Cookies:
        def set(self, *_args, **_kwargs):
            pass

    class Response:
        def __init__(self, *, status_code=200, headers=None, payload=None, text=""):
            self.status_code = status_code
            self.headers = headers or {}
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.cookies = Cookies()
            self.get_calls = 0

        def get(self, url, **_kwargs):
            self.get_calls += 1
            if self.get_calls == 1:
                return Response(text="OAuth session is still settling")
            state = parse_qs(urlparse(url).query)["state"][0]
            callback = f"{CODEX_REDIRECT_URI}?code=retry-code&state={state}"
            return Response(status_code=302, headers={"location": callback})

        @staticmethod
        def post(_url, **_kwargs):
            return Response(payload={
                "access_token": "retry-access",
                "refresh_token": "retry-refresh",
            })

    delays = []
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.time.sleep", delays.append
    )
    session = Session()

    result = mint_chatgpt_refresh_token_from_session(
        "session-token=active",
        session=session,
        authorization_attempts=3,
        retry_delay_seconds=2,
    )

    assert result["state"] == "valid"
    assert result["tokens"]["refresh_token"] == "retry-refresh"
    assert session.get_calls == 2
    assert delays == [2.0]


def _account_chooser_html() -> str:
    # Minimal React Router flattened payload with an outer session and two
    # unified login sessions. The wanted account is intentionally not first.
    payload = [
        {"_1": 2},
        "loaderData",
        {"_3": 4},
        "routes/choose-an-account",
        {"_5": 6},
        "unified_sessions",
        [7, 12],
        {"_8": 9, "_10": 11},
        "id",
        "authsess_wrong",
        "username",
        "other@example.com",
        {"_8": 13, "_10": 14},
        "authsess_right",
        "target@example.com",
    ]
    serialized = json.dumps(payload, separators=(",", ":"))
    argument = json.dumps(serialized)
    return (
        "<html><script>"
        f"window.__reactRouterContext.streamController.enqueue({argument})"
        "</script></html>"
    )


def test_account_chooser_extracts_email_matched_unified_session():
    from platforms.chatgpt.credential_checks import _extract_unified_session_id

    assert (
        _extract_unified_session_id(_account_chooser_html(), "target@example.com")
        == "authsess_right"
    )
    assert (
        _extract_unified_session_id(_account_chooser_html(), "missing@example.com")
        == ""
    )


def test_account_chooser_accepts_structured_username_from_current_auth_build():
    from platforms.chatgpt.credential_checks import _extract_unified_session_id

    email = "structured@example.com"
    payload = [
        {"_1": 2},
        "loaderData",
        {"_3": 4},
        "routes/layouts/client-auth-session-layout/layout",
        {"_5": 6},
        "session",
        {"_7": 8, "_9": 10, "_13": 14},
        "session_id",
        "authsess_structured",
        "username",
        {"_11": 12, "_13": 14},
        "kind",
        "email",
        "value",
        email,
    ]
    serialized = json.dumps(payload, separators=(",", ":"))
    html = (
        "<script>window.__reactRouterContext.streamController.enqueue("
        f"{json.dumps(serialized)})</script>"
    )

    assert _extract_unified_session_id(html, email) == "authsess_structured"


def test_silent_oauth_login_required_enters_account_selection(monkeypatch):
    from platforms.chatgpt import credential_checks
    from platforms.chatgpt.credential_checks import mint_chatgpt_refresh_token_from_session

    class Cookies:
        def set(self, *_args):
            pass

        def get(self, _name):
            return "device-2"

    class Response:
        status_code = 302
        text = ""

        def __init__(self, location):
            self.headers = {"location": location}

        @staticmethod
        def json():
            return {}

    class Session:
        cookies = Cookies()

        def get(self, url, **_kwargs):
            params = parse_qs(urlparse(url).query)
            state = params["state"][0]
            return Response(
                f"{CODEX_REDIRECT_URI}?error=login_required&state={state}"
            )

    fallback_calls = []

    def fallback(_session, *, oauth_start, **_kwargs):
        fallback_calls.append(oauth_start)
        return f"{CODEX_REDIRECT_URI}?code=fallback-code&state={oauth_start.state}"

    monkeypatch.setattr(
        credential_checks,
        "_authorization_code_via_account_selection",
        fallback,
    )
    monkeypatch.setattr(
        credential_checks,
        "submit_callback_url",
        lambda **_kwargs: json.dumps({
            "access_token": "fallback-access",
            "refresh_token": "fallback-refresh",
        }),
    )

    result = mint_chatgpt_refresh_token_from_session(
        {"session": "active"},
        session=Session(),
        sentinel_client=object(),
        authorization_attempts=1,
    )

    assert result["state"] == "valid"
    assert result["tokens"]["refresh_token"] == "fallback-refresh"
    assert len(fallback_calls) == 1
    assert parse_qs(urlparse(fallback_calls[0].auth_url).query)["prompt"] == ["login"]


def test_web_session_selects_existing_account_after_silent_oauth_error():
    from platforms.chatgpt.constants import OPENAI_AUTH
    from platforms.chatgpt.credential_checks import mint_chatgpt_refresh_token_from_session

    class Cookies:
        def __init__(self):
            self.values = {}

        def set(self, name, value):
            self.values[name] = value

        def get(self, name):
            return self.values.get(name)

    class Response:
        def __init__(self, *, status_code=200, headers=None, payload=None, text="", url=""):
            self.status_code = status_code
            self.headers = headers or {}
            self._payload = payload or {}
            self.text = text
            self.url = url

        def json(self):
            return self._payload

    class Sentinel:
        def __init__(self):
            self.calls = []

        def build_headers(self, device_id, flow):
            self.calls.append((device_id, flow))
            return {"openai-sentinel-token": "v8-proof"}

    class Session:
        def __init__(self):
            self.cookies = Cookies()
            self.get_calls = []
            self.session_select = None

        def get(self, url, **_kwargs):
            self.get_calls.append(url)
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if parsed.path == "/oauth/authorize" and params.get("prompt") == ["none"]:
                return Response(
                    status_code=302,
                    headers={"location": f"{OPENAI_AUTH}/error"},
                    url=url,
                )
            if parsed.path == "/error":
                return Response(text="OAuth error page", url=url)
            if parsed.path == "/oauth/authorize" and params.get("prompt") == ["login"]:
                return Response(
                    status_code=302,
                    headers={"location": f"{OPENAI_AUTH}/choose-an-account"},
                    url=url,
                )
            if parsed.path == "/choose-an-account":
                return Response(text=_account_chooser_html(), url=url)
            if parsed.path == "/api/accounts/client_auth_session_dump":
                return Response(payload={"client_auth_session": {}}, url=url)
            raise AssertionError(f"unexpected GET {url}")

        def post(self, url, **kwargs):
            parsed = urlparse(url)
            if parsed.path == "/api/accounts/session/select":
                self.session_select = kwargs
                state = next(
                    parse_qs(urlparse(item).query)["state"][0]
                    for item in reversed(self.get_calls)
                    if urlparse(item).path == "/oauth/authorize"
                    and parse_qs(urlparse(item).query).get("prompt") == ["login"]
                )
                return Response(payload={
                    "continue_url": f"{CODEX_REDIRECT_URI}?code=selected-code&state={state}"
                })
            if url == OAUTH_TOKEN_URL:
                return Response(payload={
                    "access_token": "selected-access",
                    "refresh_token": "selected-refresh",
                    "id_token": "selected-id",
                })
            raise AssertionError(f"unexpected POST {url}")

    sentinel = Sentinel()
    session = Session()
    result = mint_chatgpt_refresh_token_from_session(
        {"session-token": "active", "oai-did": "device-1"},
        session=session,
        email="target@example.com",
        device_id="device-1",
        sentinel_client=sentinel,
        authorization_attempts=1,
    )

    assert result["state"] == "valid"
    assert result["tokens"]["refresh_token"] == "selected-refresh"
    assert session.session_select["json"] == {"session_id": "authsess_right"}
    assert session.session_select["headers"]["openai-sentinel-token"] == "v8-proof"
    assert sentinel.calls == [("device-1", "authorize_continue")]


def test_live_registration_session_skips_silent_oauth_before_account_selection(monkeypatch):
    from platforms.chatgpt import credential_checks
    from platforms.chatgpt.credential_checks import mint_chatgpt_refresh_token_from_session

    class Cookies:
        @staticmethod
        def get(_name):
            return "live-device"

    class Session:
        cookies = Cookies()

    starts = []

    def selection(_session, *, oauth_start, **_kwargs):
        starts.append(oauth_start)
        return f"{CODEX_REDIRECT_URI}?code=live-code&state={oauth_start.state}"

    monkeypatch.setattr(
        credential_checks,
        "_authorization_code_via_account_selection",
        selection,
    )
    monkeypatch.setattr(
        credential_checks,
        "_authorization_code_from_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("prompt=none must not run for a live registration session")
        ),
    )
    monkeypatch.setattr(
        credential_checks,
        "submit_callback_url",
        lambda **_kwargs: json.dumps({
            "access_token": "live-access",
            "refresh_token": "live-refresh",
        }),
    )

    result = mint_chatgpt_refresh_token_from_session(
        {"session": "active"},
        session=Session(),
        sentinel_client=object(),
        prefer_account_selection=True,
    )

    assert result["state"] == "valid"
    assert len(starts) == 1
    assert parse_qs(urlparse(starts[0].auth_url).query)["prompt"] == ["login"]


def test_live_registration_reports_add_phone_as_rt_failure_without_sms_calls():
    from platforms.chatgpt.constants import OPENAI_AUTH
    from platforms.chatgpt.credential_checks import mint_chatgpt_refresh_token_from_session

    class Cookies:
        @staticmethod
        def get(_name):
            return "device-1"

    class Response:
        def __init__(self, *, status_code=200, headers=None, payload=None, text="", url=""):
            self.status_code = status_code
            self.headers = headers or {}
            self._payload = payload or {}
            self.text = text
            self.url = url

        def json(self):
            return self._payload

    class Sentinel:
        def __init__(self):
            self.calls = []

        def build_headers(self, device_id, flow):
            self.calls.append((device_id, flow))
            return {"openai-sentinel-token": "v8-proof"}

    class Session:
        cookies = Cookies()

        def __init__(self):
            self.paths = []

        def get(self, url, **_kwargs):
            path = urlparse(url).path
            self.paths.append(path)
            if path == "/oauth/authorize":
                return Response(
                    status_code=302,
                    headers={"location": f"{OPENAI_AUTH}/choose-an-account"},
                    url=url,
                )
            if path == "/choose-an-account":
                return Response(text=_account_chooser_html(), url=url)
            if path == "/api/accounts/client_auth_session_dump":
                raise AssertionError("add_phone must stop before the session dump")
            raise AssertionError(f"unexpected GET {url}")

        def post(self, url, **_kwargs):
            path = urlparse(url).path
            self.paths.append(path)
            if path == "/api/accounts/session/select":
                return Response(
                    payload={
                        "page": {"type": "add_phone"},
                        "continue_url": "/add-phone",
                    }
                )
            raise AssertionError(f"unexpected POST {url}")

    sentinel = Sentinel()
    session = Session()
    result = mint_chatgpt_refresh_token_from_session(
        {"session": "active"},
        session=session,
        email="target@example.com",
        device_id="device-1",
        sentinel_client=sentinel,
        prefer_account_selection=True,
    )

    assert result["state"] == "unknown"
    assert "要求手机号验证" in result["message"]
    assert "RT 获取失败" in result["message"]
    assert result["tokens"] == {}
    assert "/api/accounts/client_auth_session_dump" not in session.paths
    assert not any(path.startswith("/api/accounts/add-phone") for path in session.paths)
    assert not any(path.startswith("/api/accounts/phone-otp") for path in session.paths)
    assert sentinel.calls == [("device-1", "authorize_continue")]
