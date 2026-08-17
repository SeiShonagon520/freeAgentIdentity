from __future__ import annotations

from application import tasks as tasks_module
from application.tasks import _resolve_registration_proxy_for_platform


def test_chatgpt_registration_uses_explicit_proxy_without_proxy_pool():
    calls = []
    proxy = _resolve_registration_proxy_for_platform(
        "chatgpt",
        explicit_proxy="http://explicit-proxy.example:8080",
        proxy_getter=lambda: calls.append("called") or "http://pool-proxy.example:8080",
    )
    assert proxy == "http://explicit-proxy.example:8080"
    assert calls == []


def test_chatgpt_registration_uses_local_network_when_proxy_is_blank():
    calls = []
    proxy = _resolve_registration_proxy_for_platform(
        "chatgpt",
        explicit_proxy="  ",
        proxy_getter=lambda: calls.append("called") or "http://pool-proxy.example:8080",
    )
    assert proxy is None
    assert calls == []


def test_chatgpt_registration_creates_a_multi_exit_allocator(monkeypatch):
    captured = {}

    class Allocator:
        node_count = 12
        slot_count = 50

    class Logger:
        task_id = "proxy-node-task"
        finished = None
        messages: list[str] = []

        def set_progress(self, *_args):
            pass

        def log(self, message, **_kwargs):
            self.messages.append(message)

        def finish(self, status, *, error=""):
            self.finished = (status, error)

    monkeypatch.setattr(
        "core.mihomo_client.mihomo_client.create_registration_allocator",
        lambda preferred_node=None, **_kwargs: captured.update({"preferred": preferred_node}) or Allocator(),
    )
    monkeypatch.setattr(
        tasks_module,
        "get",
        lambda _platform: (_ for _ in ()).throw(RuntimeError("stop after allocator setup")),
    )

    logger = Logger()
    tasks_module._execute_register_task(
        {
            "count": 1,
            "concurrency": 1,
            "proxy_node": "Node A",
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert captured["preferred"] == "Node A"
    assert any("Mihomo multi-exit pool ready" in message for message in logger.messages)
