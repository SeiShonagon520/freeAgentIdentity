from __future__ import annotations

import pytest

from core.mihomo_client import MihomoClient, MihomoNodeError, MihomoUnavailableError
from deploy.mihomo.augment_slots import augment_config


def _controller_response(method: str, path: str, **kwargs):
    if method == "GET" and path == "/proxies/REGISTER-US":
        return {
            "name": "REGISTER-US",
            "type": "Selector",
            "now": "US Fast",
            "all": ["US Offline", "US Fast", "US Untested"],
        }
    if method == "GET" and path == "/proxies":
        return {
            "proxies": {
                "US Fast": {
                    "type": "VLESS",
                    "alive": True,
                    "udp": True,
                    "history": [{"time": "2026-07-30T03:00:00Z", "delay": 82}],
                },
                "US Offline": {
                    "type": "Trojan",
                    "alive": False,
                    "history": [{"time": "2026-07-30T03:00:00Z", "delay": 0}],
                },
                "US Untested": {"type": "Shadowsocks", "history": []},
            }
        }
    if method == "PUT" and path == "/proxies/REGISTER-US":
        assert kwargs["json"] == {"name": "US Fast"}
        return {}
    raise AssertionError(f"unexpected controller call: {method} {path}")


def test_mihomo_lists_node_status_and_activates_selected_node(monkeypatch):
    client = MihomoClient(
        controller_url="http://mihomo.test:9090",
        proxy_url="http://mihomo.test:7890",
        group="REGISTER-US",
    )
    calls: list[tuple[str, str]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path))
        return _controller_response(method, path, **kwargs)

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.list_nodes()
    assert result["available"] is True
    assert result["selected"] == "US Fast"
    assert [node["name"] for node in result["nodes"]] == [
        "US Fast",
        "US Untested",
        "US Offline",
    ]
    assert result["nodes"][0] == {
        "name": "US Fast",
        "type": "VLESS",
        "alive": True,
        "delay": 82,
        "last_test": "2026-07-30T03:00:00Z",
        "udp": True,
        "selected": True,
    }

    assert client.activate_node("US Fast") == "http://mihomo.test:7890"
    assert ("PUT", "/proxies/REGISTER-US") in calls


def test_mihomo_rejects_offline_node(monkeypatch):
    client = MihomoClient(group="REGISTER-US")
    monkeypatch.setattr(client, "_request", _controller_response)

    with pytest.raises(MihomoNodeError, match="当前不可用"):
        client.validate_node("US Offline")


def test_mihomo_healthy_candidates_skip_dead_selected_node(monkeypatch):
    client = MihomoClient(group="REGISTER-US")
    monkeypatch.setattr(
        client,
        "list_nodes",
        lambda **_kwargs: {
            "selected": "US Offline",
            "nodes": [
                {"name": "US Offline", "alive": False, "delay": None},
                {"name": "US Slow", "alive": True, "delay": 180},
                {"name": "US Fast", "alive": True, "delay": 24},
            ],
        },
    )
    monkeypatch.setattr(client, "is_node_enabled", lambda _node: True)

    candidates = client.healthy_node_candidates()

    assert [item["name"] for item in candidates] == ["US Fast", "US Slow"]


def test_refresh_login_route_switches_from_dead_mihomo_selected_node(monkeypatch):
    import application.tasks as tasks_module
    from core.mihomo_client import mihomo_client

    calls = []
    logs = []

    monkeypatch.setenv("MIHOMO_PROXY_URL", "http://mihomo:7890")
    monkeypatch.setattr(
        mihomo_client,
        "healthy_node_candidates",
        lambda **_kwargs: [{"name": "US Fast", "alive": True, "delay": 23}],
    )
    monkeypatch.setattr(
        mihomo_client,
        "activate_node",
        lambda name: calls.append(("activate", name)) or "http://mihomo:7890",
    )

    def probe(proxy):
        calls.append(("probe", proxy))
        return (proxy == "http://mihomo:7890" and calls.count(("probe", proxy)) > 1, "HTTP 200" if calls.count(("probe", proxy)) > 1 else "ConnectionError")

    monkeypatch.setattr(tasks_module, "_probe_chatgpt_login_route", probe)
    logger = type(
        "LoggerStub",
        (),
        {"log": lambda _self, message, **_kwargs: logs.append(message)},
    )()

    proxy = tasks_module._resolve_refresh_login_proxy("", logger=logger)

    assert proxy == "http://mihomo:7890"
    assert ("activate", "US Fast") in calls
    assert any("自动切换到健康节点" in message for message in logs)


def test_mihomo_reads_subscription_node_details_from_provider_api(monkeypatch):
    client = MihomoClient(group="REGISTER-US")

    def fake_request(method, path, **_kwargs):
        assert method == "GET"
        if path == "/proxies/REGISTER-US":
            return {"now": "US Provider", "all": ["US Provider"]}
        if path == "/proxies":
            return {"proxies": {}}
        if path == "/providers/proxies":
            return {
                "providers": {
                    "subscription": {
                        "proxies": [
                            {
                                "name": "US Provider",
                                "type": "Vless",
                                "alive": True,
                                "udp": True,
                                "history": [
                                    {"time": "2026-07-30T03:01:00Z", "delay": 24}
                                ],
                            }
                        ]
                    }
                }
            }
        raise AssertionError(f"unexpected controller call: {method} {path}")

    monkeypatch.setattr(client, "_request", fake_request)

    node = client.list_nodes()["nodes"][0]
    assert node == {
        "name": "US Provider",
        "type": "Vless",
        "alive": True,
        "delay": 24,
        "last_test": "2026-07-30T03:01:00Z",
        "udp": True,
        "selected": True,
    }


def test_mihomo_hides_subscription_plan_metadata(monkeypatch):
    client = MihomoClient(group="REGISTER-ALL")

    def fake_request(method, path, **_kwargs):
        if path == "/proxies/REGISTER-ALL":
            return {
                "now": "Node A",
                "all": [
                    "\u5269\u4f59\u6d41\u91cf\uff1a994 GB",
                    "\u8ddd\u79bb\u4e0b\u6b21\u91cd\u7f6e\u5269\u4f59\uff1a4 \u5929",
                    "\u5957\u9910\u5230\u671f\uff1a2026-09-05",
                    "Node A",
                ],
            }
        if path == "/proxies":
            return {
                "proxies": {
                    "Node A": {"type": "VLESS", "alive": True},
                }
            }
        raise AssertionError(f"unexpected controller call: {method} {path}")

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.list_nodes()
    assert [node["name"] for node in result["nodes"]] == ["Node A"]


def test_proxy_nodes_api_returns_safe_unavailable_response(client, monkeypatch):
    monkeypatch.setattr(
        "api.proxy_nodes.mihomo_client.list_nodes",
        lambda **_kwargs: (_ for _ in ()).throw(MihomoUnavailableError("controller down")),
    )

    response = client.get("/api/proxy-nodes")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["nodes"] == []
    assert "controller down" in response.json()["error"]


def test_register_api_accepts_a_healthy_mihomo_node(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "api.task_commands.mihomo_client.validate_node",
        lambda name: {"name": name, "alive": True},
    )
    monkeypatch.setattr(
        "api.task_commands.command_service.create_register_task",
        lambda payload: captured.update(payload) or {"task_id": "task_with_proxy"},
    )

    response = client.post(
        "/api/tasks/register",
        json={
            "count": 1,
            "concurrency": 2,
            "proxy_node": "US Fast",
            "extra": {"mail_provider": "domain_inbucket"},
        },
    )

    assert response.status_code == 200
    assert captured["proxy_node"] == "US Fast"
    assert captured["proxy"] is None


def test_401_check_api_accepts_a_healthy_login_proxy_node(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "api.account_checks.mihomo_client.validate_node",
        lambda name: {"name": name, "alive": True},
    )
    monkeypatch.setattr(
        "api.account_checks.service.check_refresh_tokens_async",
        lambda platform, concurrency, proxy_node=None, browser=False: captured.update(
            {
                "platform": platform,
                "concurrency": concurrency,
                "proxy_node": proxy_node,
                "browser": browser,
            }
        ) or {"task_id": "task_check_with_proxy"},
    )

    response = client.post(
        "/api/accounts/check-refresh-tokens",
        json={
            "platform": "chatgpt",
            "concurrency": 3,
            "proxy_node": "US Fast",
        },
    )

    assert response.status_code == 200
    assert captured == {
        "platform": "chatgpt",
        "concurrency": 3,
        "proxy_node": "US Fast",
        "browser": True,
    }


def test_401_check_api_accepts_targeted_account_ids(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "api.account_checks.service.check_refresh_tokens_async",
        lambda platform, concurrency, proxy_node=None, browser=True, account_ids=None: captured.update(
            {
                "platform": platform,
                "concurrency": concurrency,
                "proxy_node": proxy_node,
                "browser": browser,
                "account_ids": account_ids,
            }
        ) or {"task_id": "task_targeted"},
    )

    response = client.post(
        "/api/accounts/check-refresh-tokens",
        json={"account_ids": [3723], "concurrency": 1, "browser": False},
    )

    assert response.status_code == 200
    assert captured == {
        "platform": "chatgpt",
        "concurrency": 1,
        "proxy_node": None,
        "browser": False,
        "account_ids": [3723],
    }


def test_register_api_rejects_manual_proxy_and_node_together(client):
    response = client.post(
        "/api/tasks/register",
        json={
            "proxy": "http://manual-proxy.test:8080",
            "proxy_node": "US Fast",
            "extra": {"mail_provider": "domain_inbucket"},
        },
    )

    assert response.status_code == 400
    assert "不能同时选择" in response.json()["detail"]
def test_mihomo_registration_allocator_keeps_worker_slots_sticky(monkeypatch):
    client = MihomoClient(
        controller_url="http://mihomo.test:9090",
        proxy_url="http://mihomo.test:7890",
        group="REGISTER-ALL",
    )
    client.slot_count = 2
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET" and path == "/proxies/REGISTER-ALL":
            return {"now": "Node A", "all": ["Node A", "Node B"]}
        if method == "GET" and path == "/proxies":
            return {
                "proxies": {
                    "Node A": {"type": "VLESS", "alive": True},
                    "Node B": {"type": "Trojan", "alive": True},
                }
            }
        if method == "PUT" and path.startswith("/proxies/REGISTER-SLOT-"):
            return {}
        raise AssertionError((method, path))

    monkeypatch.setattr(client, "_request", fake_request)
    allocator = client.create_registration_allocator(preferred_node="Node A")
    first = allocator.acquire()
    second = allocator.acquire()

    assert first.proxy == "http://mihomo.test:7901"
    assert second.proxy == "http://mihomo.test:7902"
    assert first.node == "Node A"
    assert second.node == "Node B"

    old_node = first.node
    first.rotate()
    assert first.node == "Node B"
    assert first.node != old_node
    # A transient timeout on every node must not permanently exhaust the
    # allocator.  Node A remains reusable after its cooldown fallback.
    second.rotate()
    assert second.node == "Node A"
    assert old_node not in allocator._failed_nodes
    assert any(path == "/proxies/REGISTER-SLOT-01" for _, path, _ in calls)

    first.release()
    second.release()


def test_registration_worker_never_revisits_a_timed_out_node(monkeypatch):
    client = MihomoClient(
        controller_url="http://mihomo.test:9090",
        proxy_url="http://mihomo.test:7890",
        group="REGISTER-ALL",
    )
    client.slot_count = 1
    monkeypatch.setattr(
        client,
        "list_nodes",
        lambda **_kwargs: {
            "nodes": [
                {"name": "Node A", "alive": True},
                {"name": "Node B", "alive": True},
                {"name": "Node C", "alive": True},
            ]
        },
    )
    monkeypatch.setattr(client, "is_node_enabled", lambda _node: True)
    monkeypatch.setattr(client, "activate_slot_node", lambda *_args, **_kwargs: "")

    allocator = client.create_registration_allocator()
    lease = allocator.acquire()
    visited = [lease.node]
    visited.append(lease.rotate() and lease.node)
    visited.append(lease.rotate() and lease.node)

    assert visited == ["Node A", "Node B", "Node C"]
    assert lease.attempted_nodes == set(visited)
    with pytest.raises(MihomoNodeError):
        lease.rotate()
    lease.release()


def test_registration_preflight_excludes_chatgpt_vpn_blocked_nodes(monkeypatch):
    client = MihomoClient(
        controller_url="http://mihomo.test:9090",
        proxy_url="http://mihomo.test:7890",
        group="REGISTER-ALL",
    )
    monkeypatch.setattr(
        client,
        "list_nodes",
        lambda **_kwargs: {
            "nodes": [
                {"name": "Blocked", "alive": True},
                {"name": "Good", "alive": True},
            ]
        },
    )
    monkeypatch.setattr(client, "is_node_enabled", lambda _node: True)
    monkeypatch.setattr(
        client,
        "preflight_registration_nodes",
        lambda _nodes: {
            "Blocked": {
                "eligible": False,
                "classification": "vpn_block",
                "detail": "unable to load site",
            },
            "Good": {
                "eligible": True,
                "classification": "ok",
                "detail": "HTTP 200",
            },
        },
    )
    monkeypatch.setattr(client, "activate_slot_node", lambda slot, node: client.slot_proxy_url(slot))

    allocator = client.create_registration_allocator(preflight=True)
    lease = allocator.acquire()
    try:
        assert allocator.nodes == ["Good"]
        assert lease.node == "Good"
    finally:
        lease.release()


def test_allocator_rotation_skips_a_node_that_went_offline(monkeypatch):
    client = MihomoClient(
        controller_url="http://mihomo.test:9090",
        proxy_url="http://mihomo.test:7890",
        group="REGISTER-ALL",
    )
    client.slot_count = 2
    monkeypatch.setattr(
        client,
        "list_nodes",
        lambda **_kwargs: {
            "nodes": [
                {"name": "Node A", "alive": True},
                {"name": "Node B", "alive": True},
                {"name": "Node C", "alive": True},
            ]
        },
    )
    monkeypatch.setattr(client, "is_node_enabled", lambda _node: True)
    activated = []

    def activate(_slot, node):
        activated.append(node)
        if node == "Node B":
            raise MihomoNodeError("Node B went offline")
        return client.slot_proxy_url(1)

    monkeypatch.setattr(client, "activate_slot_node", activate)
    allocator = client.create_registration_allocator(preferred_node="Node A")
    lease = allocator.acquire()
    try:
        assert lease.node == "Node A"
        lease.rotate()
        assert lease.node == "Node C"
        assert activated == ["Node A", "Node B", "Node C"]
    finally:
        lease.release()


def test_mihomo_config_generates_all_node_groups_and_listeners():
    config = augment_config(
        "proxy-groups:\n  - name: OLD\n    type: select\nrules:\n  - MATCH,REGISTER-US\n",
        slot_count=3,
        port_base=7900,
    )

    assert "name: REGISTER-ALL" in config
    assert "name: REGISTER-US" in config
    assert "name: REGISTER-SLOT-03" in config
    assert "name: REGISTER-IN-03" in config
    assert "port: 7903" in config
    assert "- MATCH,REGISTER-ALL" in config
    assert "name: OLD" not in config
