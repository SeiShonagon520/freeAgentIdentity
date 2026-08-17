from __future__ import annotations

from types import SimpleNamespace

from application import tasks as tasks_module


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


def test_headless_registration_preserves_explicit_totp_opt_out(monkeypatch):
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
            "extra": {
                "mail_provider": "local_ms_pool",
                "bind_totp_2fa": False,
            },
        }
    )

    assert captured["payload"]["extra"]["bind_totp_2fa"] is False


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
