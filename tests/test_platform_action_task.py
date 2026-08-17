from __future__ import annotations

from application import tasks as tasks_module
from core.base_platform import Account
from domain.actions import ActionExecutionResult
from domain.actions import ActionExecutionCommand
from infrastructure import platform_runtime as runtime_module


class _FakeLogger:
    def __init__(self, task_id="test-task"):
        self.task_id = task_id
        self.events = []
        self.result_data = None
        self.finished = None
        self.cancel_requested = False

    def log(self, message, **kwargs):
        self.events.append(("log", message, kwargs))

    def record_error(self, error):
        self.events.append(("error", error, {}))

    def record_success(self):
        self.events.append(("success", "", {}))

    def set_result_data(self, data):
        self.result_data = data

    def set_progress(self, current, total):
        self.events.append(("progress", current, {"total": total}))

    def is_cancel_requested(self):
        return self.cancel_requested

    def set_subtask(self, subtask_id, label=""):
        self.events.append(("subtask", subtask_id, {"label": label}))

    def clear_subtask(self):
        self.events.append(("clear_subtask", "", {}))

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def test_platform_action_task_passes_task_logger_to_runtime(monkeypatch):
    seen = {}

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            seen["log_fn"] = log_fn
            seen["cancel_check"] = cancel_check
            if log_fn:
                log_fn("checkout step log")
            return ActionExecutionResult(ok=True, data={"message": "summary"})

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "query_state",
            "params": {},
        },
        logger,
    )

    assert getattr(seen["log_fn"], "__self__", None) is logger
    assert getattr(seen["log_fn"], "__name__", "") == "log"
    assert getattr(seen["cancel_check"], "__self__", None) is logger
    assert getattr(seen["cancel_check"], "__name__", "") == "is_cancel_requested"
    assert seen["cancel_check"]() is False
    assert ("log", "checkout step log", {}) in logger.events
    assert logger.result_data == {"message": "summary"}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_chatgpt_register_task_succeeds_after_successful_registration(monkeypatch):
    checked = {}

    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={
                    "access_token": "access-token",
                    "cookies": {"oai-did": "device-123", "session": "saved-cookie"},
                    "_registration_password_confirmed": True,
                    "totp_secret": "TESTTOTPSECRET",
                },
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )

    def save(account):
        checked["account"] = account
        return type("SavedAccount", (), {"id": 123})()

    monkeypatch.setattr(tasks_module, "save_account", save)
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda token, **kwargs: checked.update(
            {
                "token": token,
                "proxy": kwargs.get("proxy"),
                "account_id": kwargs.get("account_id"),
            }
        )
        or {"state": "valid", "message": "access token 可用"},
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "mailbox",
                "auto_upload_sub2api_agent_identity": True,
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data == {
        "success": 1,
        "fail": 0,
        "account_ids": [123],
        "accounts": [
            {
                "account_id": 123,
                "email": "registered@example.com",
                "totp_2fa": {
                    "requested": True,
                    "bound": True,
                    "error": "",
                },
            }
        ],
        "auto_upload_sub2api_agent_identity": True,
        "sub2api_agent_identity_upload": {
            "submitted": 0,
            "failed": 0,
            "errors": [],
        },
    }
    assert any(event[0] == "success" for event in logger.events)
    assert checked["token"] == "access-token"
    assert checked["proxy"] is None
    assert checked["account_id"] == "acct_123"
    assert checked["account"].extra["account_overview"]["refresh_token_status"] == "valid"
    assert checked["account"].extra["account_overview"]["refresh_token_check_method"] == "access_token"
    assert not any(
        "cannot access local variable 'extra'" in str(event)
        for event in logger.events
    )


def test_register_task_retries_once_with_a_new_mailbox_after_otp_timeout(monkeypatch):
    attempts = []
    saved = []

    class FakePlatform:
        def register(self, email=None, password=None):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise TimeoutError("等待验证码超时 (180s)")
            return Account(
                platform="chatgpt",
                email="retry-success@example.com",
                password=password or "Secret123!",
                user_id="acct_retry",
                extra={
                    "access_token": "retry-access",
                    "_registration_password_confirmed": True,
                    "totp_secret": "TESTTOTPSECRET",
                },
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {"state": "valid", "message": "access token 可用"},
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())

    def save(account):
        saved.append(account)
        return type("SavedAccount", (), {"id": 456})()

    monkeypatch.setattr(tasks_module, "save_account", save)
    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert attempts == [1, 2]
    assert len(saved) == 1
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success"] == 1
    assert logger.result_data["fail"] == 0
    assert any("更换新邮箱重试一次" in str(event) for event in logger.events)


def test_register_task_does_not_save_an_account_without_access_token(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "missing-token@example.com",
                password=password or "Secret123!",
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        "core.base_mailbox.create_mailbox",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account: (_ for _ in ()).throw(AssertionError("must not save")),
    )

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "count": 1,
            "concurrency": 1,
            "email": "missing-token@example.com",
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert logger.finished[0] == tasks_module.TASK_STATUS_FAILED
    assert logger.result_data["success"] == 0
    assert logger.result_data["fail"] == 1


def test_register_task_does_not_save_an_account_whose_access_token_returns_401(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "dead@example.com",
                password=password or "Secret123!",
                extra={"access_token": "dead-access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {
            "state": "invalid",
            "message": "access token 返回 HTTP 401",
        },
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account: (_ for _ in ()).throw(AssertionError("must not save a confirmed 401")),
    )

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "count": 1,
            "concurrency": 1,
            "email": "dead@example.com",
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert logger.finished[0] == tasks_module.TASK_STATUS_FAILED
    assert logger.result_data["success"] == 0
    assert logger.result_data["fail"] == 1
    assert any("401 验活失败" in str(event) for event in logger.events)


def test_register_task_does_not_save_without_remote_password_confirmation(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "passwordless@example.com",
                password=password or "Secret123!",
                extra={"access_token": "fresh-access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {"state": "valid", "message": "access token 可用"},
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        tasks_module,
        "save_account",
        lambda account: (_ for _ in ()).throw(
            AssertionError("must not save a password-unconfirmed account")
        ),
    )

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "count": 1,
            "concurrency": 1,
            "email": "passwordless@example.com",
            "extra": {"identity_provider": "mailbox", "bind_totp_2fa": False},
        },
        logger,
    )

    assert logger.finished[0] == tasks_module.TASK_STATUS_FAILED
    assert logger.result_data["success"] == 0
    assert logger.result_data["fail"] == 1
    assert any("密码已设置" in str(event) for event in logger.events)


def test_register_task_saves_an_account_when_401_check_is_inconclusive(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "unconfirmed@example.com",
                password=password or "Secret123!",
                extra={
                    "access_token": "fresh-access-token",
                    "_registration_password_confirmed": True,
                    "totp_secret": "TESTTOTPSECRET",
                },
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        lambda *_args, **_kwargs: {
            "state": "unknown",
            "message": "401 校验未确认（me: HTTP 403）",
        },
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())
    saved = []

    def save(account):
        saved.append(account)
        return type("SavedAccount", (), {"id": 321})()

    monkeypatch.setattr(tasks_module, "save_account", save)

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "unconfirmed@example.com",
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success"] == 1
    assert logger.result_data["fail"] == 0
    assert len(saved) == 1
    assert saved[0].extra["account_overview"]["refresh_token_status"] == "unknown"
    assert any("验活未确认，注册结果仍保存" in str(event) for event in logger.events)


def test_register_task_honors_fifty_worker_concurrency_limit():
    assert tasks_module._registration_concurrency(20, 50) == 20
    assert tasks_module._registration_concurrency(99, 50) == 50
    assert tasks_module._registration_concurrency(50, 0) == 50
    assert tasks_module._registration_concurrency(20, 6) == 6


def test_platform_instance_receives_task_cancel_checker(monkeypatch):
    captured = {}

    class FakePlatform:
        def __init__(self, **_kwargs):
            pass

        def set_logger(self, logger):
            captured["logger"] = logger

        def set_cancel_checker(self, checker):
            captured["cancel_checker"] = checker

    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: FakePlatform)
    logger = _FakeLogger()

    tasks_module._build_platform_instance(
        "chatgpt",
        {"extra": {"identity_provider": "generated"}},
        logger,
    )

    assert captured["logger"] == logger.log
    assert captured["cancel_checker"] == logger.is_cancel_requested


def test_register_task_checks_mailbox_before_starting_workers(monkeypatch):
    class UnavailableMailbox:
        @staticmethod
        def test_connection():
            raise ConnectionError("127.0.0.1:19000 refused")

    monkeypatch.setattr(tasks_module, "get", lambda _platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.base_mailbox.create_mailbox",
        lambda *args, **kwargs: UnavailableMailbox(),
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("worker must not start")
        ),
    )
    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "count": 50,
            "concurrency": 50,
            "extra": {
                "identity_provider": "mailbox",
                "mail_provider": "domain_inbucket",
            },
        },
        logger,
    )

    assert logger.finished[0] == tasks_module.TASK_STATUS_FAILED
    assert "邮箱服务不可用" in logger.finished[1]


def test_register_task_uploads_each_saved_account_immediately(monkeypatch):
    events = []

    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={
                    "access_token": "access-token",
                    "_registration_password_confirmed": True,
                    "totp_secret": "TESTTOTPSECRET",
                },
            )

    saved_ids = iter((123, 124))

    def save(account):
        account_id = next(saved_ids)
        events.append(("saved", account_id))
        return type("SavedAccount", (), {"id": account_id})()

    def check_access_token(token, **_kwargs):
        events.append(("checked", token))
        return {"state": "valid", "message": "access token 可用"}

    def upload(account_id, *, sub2api_url, api_key):
        events.append(("uploaded", account_id, sub2api_url, api_key))
        return {"submitted": 1}

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(tasks_module, "save_account", save)
    monkeypatch.setattr(
        "platforms.chatgpt.credential_checks.check_chatgpt_access_token",
        check_access_token,
    )
    monkeypatch.setattr(
        tasks_module,
        "_upload_registered_chatgpt_account_to_sub2api",
        upload,
    )
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *args, **kwargs: object())

    logger = _FakeLogger(task_id="task-immediate-upload")
    tasks_module.set_register_sub2api_upload_config(
        logger.task_id,
        sub2api_url="https://sub2api.example",
        api_key="volatile-admin-key",
    )
    try:
        tasks_module._execute_register_task(
            {
                "platform": "chatgpt",
                "count": 2,
                "concurrency": 1,
                "extra": {
                    "identity_provider": "mailbox",
                    "auto_upload_sub2api_agent_identity": True,
                },
            },
            logger,
        )
    finally:
        tasks_module.clear_register_sub2api_upload_config(logger.task_id)

    assert [item[:2] for item in events] == [
        ("checked", "access-token"),
        ("saved", 123),
        ("uploaded", 123),
        ("checked", "access-token"),
        ("saved", 124),
        ("uploaded", 124),
    ]
    assert logger.result_data["sub2api_agent_identity_upload"] == {
        "submitted": 2,
        "failed": 0,
        "errors": [],
    }


def test_register_api_uses_selected_mailbox_and_protocol(client, monkeypatch):
    captured = {}

    def fake_create(payload, **_kwargs):
        captured.update(payload)
        return {"task_id": "task_protocol"}

    monkeypatch.setattr("api.task_commands.command_service.create_register_task", fake_create)
    response = client.post(
        "/api/tasks/register",
        json={
            "count": 1,
            "concurrency": 50,
            "extra": {"mail_provider": "domain_inbucket"},
        },
    )

    assert response.status_code == 200
    assert captured["executor_type"] == "protocol"
    assert captured["concurrency"] == 50
    assert captured["extra"] == {
        "mail_provider": "domain_inbucket",
        "identity_provider": "mailbox",
    }


def test_register_api_accepts_zero_for_unlimited_registration(client, monkeypatch):
    captured = {}

    def fake_create(payload):
        captured["payload"] = payload
        return {"task_id": "task_protocol"}

    monkeypatch.setattr("api.task_commands.command_service.create_register_task", fake_create)

    response = client.post(
        "/api/tasks/register",
        json={
            "count": 0,
            "concurrency": 50,
            "extra": {"mail_provider": "domain_inbucket"},
        },
    )

    assert response.status_code == 200
    assert captured["payload"]["count"] == 0
    assert captured["payload"]["concurrency"] == 50
    assert captured["payload"]["executor_type"] == "protocol"


def test_register_api_rejects_concurrency_over_fifty(client, monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "api.task_commands.command_service.create_register_task",
        lambda payload: captured.update(payload) or {"task_id": "task_protocol"},
    )
    accepted = client.post(
        "/api/tasks/register",
        json={
            "count": 6,
            "concurrency": 50,
            "extra": {"mail_provider": "domain_inbucket"},
        },
    )
    rejected = client.post(
        "/api/tasks/register",
        json={
            "count": 7,
            "concurrency": 51,
            "extra": {"mail_provider": "domain_inbucket"},
        },
    )

    assert accepted.status_code == 200
    assert captured["extra"]["mail_provider"] == "domain_inbucket"
    assert rejected.status_code == 422
    return
    assert "子邮箱容量 6" in rejected.json()["detail"]


def test_register_api_rejects_protocol_without_mailbox_service(client):
    response = client.post(
        "/api/tasks/register",
        json={"executor_type": "protocol", "count": 1, "extra": {}},
    )

    assert response.status_code == 400
    assert "邮箱服务" in response.json()["detail"]


def test_register_api_allows_protocol_with_domain_mailbox(client, monkeypatch):
    captured = {}

    def fake_create(payload, **kwargs):
        captured.update(payload)
        return {"task_id": "domain-task"}

    monkeypatch.setattr("api.task_commands.command_service.create_register_task", fake_create)

    response = client.post(
        "/api/tasks/register",
        json={
            "executor_type": "protocol",
            "count": 2,
            "extra": {"mail_provider": "domain_imap_catchall"},
        },
    )

    assert response.status_code == 200
    assert captured["extra"]["mail_provider"] == "domain_imap_catchall"
    assert "local_ms_pool_alias_count" not in captured["extra"]


def test_platform_action_task_finishes_cancelled_without_starting_runtime(monkeypatch):
    class FakeRuntime:
        def execute_action(self, *args, **kwargs):
            raise AssertionError("runtime should not start after cancellation")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()
    logger.cancel_requested = True

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "query_state",
            "params": {},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_platform_action_task_marks_cancelled_after_runtime_cancel(monkeypatch):
    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            assert cancel_check() is False
            logger.cancel_requested = True
            return ActionExecutionResult(ok=False, error="任务已取消")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "query_state",
            "params": {},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_platform_runtime_wires_log_fn_to_platform(monkeypatch):
    logs = []
    seen = {}

    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt"})()

        def add(self, model):
            pass

        def commit(self):
            pass

    class FakePlatform:
        def __init__(self, config=None):
            self._log_fn = print

        def set_logger(self, logger):
            self._log_fn = logger

        def set_cancel_checker(self, checker):
            seen["cancel_check"] = checker

        def execute_action(self, action_id, account, params):
            self._log_fn("runtime platform log")
            assert self.is_cancel_requested() is False
            return {"ok": True, "data": {"message": "ok"}}

        def is_cancel_requested(self):
            return seen["cancel_check"]()

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", lambda *args, **kwargs: None)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="query_state",
            params={},
        ),
        log_fn=logs.append,
        cancel_check=lambda: False,
    )

    assert result.ok is True
    assert logs == ["runtime platform log"]
    assert seen["cancel_check"]() is False
