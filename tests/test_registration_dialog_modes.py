from __future__ import annotations


def test_register_api_accepts_automatic_mihomo_pool(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "api.task_commands.mihomo_client.list_nodes",
        lambda: {"nodes": [{"name": "Node A", "alive": True}]},
    )
    monkeypatch.setattr("api.task_commands.mihomo_client.is_node_enabled", lambda _name: True)
    monkeypatch.setattr(
        "api.task_commands.command_service.create_register_task",
        lambda payload: captured.update(payload) or {"task_id": "pool-task"},
    )

    response = client.post(
        "/api/tasks/register",
        json={
            "count": 1,
            "proxy_pool": True,
            "extra": {"mail_provider": "domain_inbucket"},
        },
    )

    assert response.status_code == 200
    assert captured["proxy_pool"] is True
    assert captured["proxy_node"] is None


def test_microsoft_registration_forces_six_one_time_children(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "api.task_commands.command_service.create_register_task",
        lambda payload: captured.update(payload) or {"task_id": "microsoft-task"},
    )

    response = client.post(
        "/api/tasks/register",
        json={"count": 1, "extra": {"mail_provider": "local_ms_pool"}},
    )

    assert response.status_code == 200
    assert captured["extra"]["local_ms_pool_alias_count"] == 6
    assert captured["extra"]["local_ms_pool_allow_reuse"] == "false"


def test_registration_requires_an_explicit_mailbox_selection(client, monkeypatch):
    monkeypatch.setattr(
        "infrastructure.provider_settings_repository.ProviderSettingsRepository.get_default_provider_key",
        lambda *_args, **_kwargs: "local_ms_pool",
    )

    response = client.post("/api/tasks/register", json={"count": 1, "extra": {}})

    assert response.status_code == 400
    assert "请选择本次注册使用的邮箱服务" in response.json()["detail"]


def test_server_runtime_hides_and_rejects_har_capture(client, monkeypatch):
    monkeypatch.setenv("APP_RUNTIME_MODE", "server")

    runtime = client.get("/api/system/runtime")
    rejected = client.post("/api/tasks/register", json={"har_capture": True})

    assert runtime.status_code == 200
    assert runtime.json() == {"mode": "server", "har_capture_available": False}
    assert rejected.status_code == 400
    assert "服务器模式" in rejected.json()["detail"]
