from __future__ import annotations

from types import SimpleNamespace

from application import tasks as tasks_module
from core.account_graph import _platform_credentials_from_extra
from platforms.chatgpt.plugin import ChatGPTPlatform


def test_headless_registration_defaults_to_binding_totp(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        tasks_module,
        "create_task",
        lambda **kwargs: captured.update(kwargs) or {"task_id": "headless-task"},
    )

    tasks_module.create_register_task(
        {
            "count": 1,
            "executor_type": "headless",
            "extra": {"mail_provider": "local_ms_pool"},
        }
    )

    assert captured["payload"]["extra"]["bind_totp_2fa"] is True


def test_all_registration_modes_override_explicit_totp_opt_out(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        tasks_module,
        "create_task",
        lambda **kwargs: captured.update(kwargs) or {"task_id": "headless-task"},
    )

    tasks_module.create_register_task(
        {
            "count": 1,
            "executor_type": "protocol",
            "extra": {
                "mail_provider": "local_ms_pool",
                "bind_totp_2fa": False,
            },
        }
    )

    assert captured["payload"]["extra"]["bind_totp_2fa"] is True


def test_headed_registration_always_requires_totp(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        tasks_module,
        "create_task",
        lambda **kwargs: captured.update(kwargs) or {"task_id": "headed-task"},
    )

    tasks_module.create_register_task(
        {
            "count": 1,
            "executor_type": "headed",
            "extra": {"mail_provider": "local_ms_pool", "bind_totp_2fa": False},
        }
    )

    assert captured["payload"]["extra"]["bind_totp_2fa"] is True


def test_bind_registered_account_totp_uses_proxy_persists_and_closes(monkeypatch):
    session = SimpleNamespace(proxies={}, closed=False)
    session.close = lambda: setattr(session, "closed", True)
    monkeypatch.setattr("curl_cffi.requests.Session", lambda **_kwargs: session)
    monkeypatch.setattr(
        "platforms.chatgpt.mfa.bind_totp_2fa",
        lambda actual_session, token: {
            "activated": actual_session is session and token == "access-token",
            "secret": "TOTPSECRET",
            "result": {"success": True},
        },
    )
    persisted = {}
    monkeypatch.setattr(
        tasks_module,
        "_persist_totp_secret",
        lambda account_id, secret: persisted.update(
            {"account_id": account_id, "secret": secret}
        ),
    )

    secret = tasks_module._bind_registered_account_totp(
        SimpleNamespace(token="access-token", extra={}),
        42,
        proxy="http://127.0.0.1:19001",
    )

    assert secret == "TOTPSECRET"
    assert session.proxies == {
        "http": "http://127.0.0.1:19001",
        "https": "http://127.0.0.1:19001",
    }
    assert persisted == {"account_id": 42, "secret": "TOTPSECRET"}
    assert session.closed is True


def test_bind_registered_account_totp_does_not_persist_unconfirmed_secret(monkeypatch):
    session = SimpleNamespace(proxies={}, closed=False)
    session.close = lambda: setattr(session, "closed", True)
    monkeypatch.setattr("curl_cffi.requests.Session", lambda **_kwargs: session)
    monkeypatch.setattr(
        "platforms.chatgpt.mfa.bind_totp_2fa",
        lambda *_args, **_kwargs: {
            "activated": False,
            "secret": "UNCONFIRMED",
            "result": {"success": False},
        },
    )
    persisted = []
    monkeypatch.setattr(
        tasks_module,
        "_persist_totp_secret",
        lambda *_args: persisted.append(True),
    )

    try:
        tasks_module._bind_registered_account_totp(
            SimpleNamespace(token="access-token", extra={}),
            42,
        )
    except RuntimeError as exc:
        assert "激活未确认" in str(exc)
    else:
        raise AssertionError("unconfirmed TOTP activation must fail")

    assert persisted == []
    assert session.closed is True


def test_bind_registered_account_totp_reuses_exported_browser_cookies_before_save(monkeypatch):
    captured = {}
    session = SimpleNamespace(proxies={}, closed=False)
    session.close = lambda: setattr(session, "closed", True)

    def make_session(**kwargs):
        captured.update(kwargs)
        return session

    monkeypatch.setattr("curl_cffi.requests.Session", make_session)
    monkeypatch.setattr(
        "platforms.chatgpt.mfa.bind_totp_2fa",
        lambda *_args, **_kwargs: {
            "activated": True,
            "secret": "COOKIESESSIONSECRET",
            "result": {"success": True},
        },
    )
    persisted = []
    monkeypatch.setattr(
        tasks_module,
        "_persist_totp_secret",
        lambda *_args: persisted.append(True),
    )

    secret = tasks_module._bind_registered_account_totp(
        SimpleNamespace(
            token="access-token",
            extra={"cookies": "oai-did=device; __Secure-next-auth.session-token=session"},
        ),
        proxy="http://127.0.0.1:19001",
    )

    assert secret == "COOKIESESSIONSECRET"
    assert captured["headers"] == {
        "Cookie": "oai-did=device; __Secure-next-auth.session-token=session"
    }
    assert persisted == []
    assert session.closed is True


def test_chatgpt_result_maps_browser_totp_secret_to_platform_credentials():
    platform = object.__new__(ChatGPTPlatform)
    result = platform._map_chatgpt_result(
        {
            "email": "user@example.com",
            "access_token": "access-token",
            "totp_2fa": {
                "requested": True,
                "bound": True,
                "secret": "BROWSERBOUNDSECRET",
                "error": "",
            },
        },
        password="StrongPass123!",
    )

    assert result.extra["totp_secret"] == "BROWSERBOUNDSECRET"
    assert result.extra["_registration_password_confirmed"] is False
    credentials = _platform_credentials_from_extra(result.extra)
    assert any(
        item["key"] == "totp_secret"
        and item["value"] == "BROWSERBOUNDSECRET"
        and item["credential_type"] == "secret"
        for item in credentials
    )


def test_chatgpt_result_maps_password_confirmation_marker():
    platform = object.__new__(ChatGPTPlatform)
    result = platform._map_chatgpt_result(
        {
            "email": "user@example.com",
            "access_token": "access-token",
            "password_registered": True,
        },
        password="StrongPass123!",
    )

    assert result.extra["_registration_password_confirmed"] is True
