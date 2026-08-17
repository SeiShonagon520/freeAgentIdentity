"""Pulse-registration tests: per-node ban tracking, synchronized waves, probes."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from application import tasks as tasks_module
from application.registration_pulse import PulseConfig, PulseRegistration
from application.tasks import (
    MAX_REGISTER_CONCURRENCY,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCEEDED,
    _NO_EMAIL_MARKER,
    _POOL_EXHAUSTED_MARKER,
)
from core.base_platform import RegisterConfig
from core.mihomo_client import MihomoClient, MihomoNodeError
from core.registration.helpers import build_otp_callback


# --------------------------------------------------------------------------- fakes


class _FakeLogger:
    def __init__(self):
        self.events = []
        self.result_data = None
        self.finished = None
        self.finish_calls = []
        self.cancel_requested = False
        self._lock = threading.Lock()

    def log(self, message, **kwargs):
        with self._lock:
            self.events.append(("log", message, kwargs))

    def set_progress(self, current, total):
        with self._lock:
            self.events.append(("progress", current, total))

    def set_result_data(self, data):
        self.result_data = data

    def set_subtask(self, *_args):
        pass

    def clear_subtask(self):
        pass

    def record_error(self, *_args):
        pass

    def record_success(self):
        pass

    def is_cancel_requested(self):
        return self.cancel_requested

    def finish(self, status, *, error=""):
        self.finished = (status, error)
        self.finish_calls.append((status, error))


class _FakeAllocator:
    """Minimal allocator with the real ban-state semantics used by the controller."""

    def __init__(self, nodes=("Node A", "Node B"), slot_count=None):
        self.nodes = list(nodes)
        self.slot_count = slot_count if slot_count is not None else len(self.nodes)
        self._banned = set()
        self._strikes = {}
        self._lock = threading.Lock()
        self.banned_calls: list[str] = []
        self.unblock_calls: list[str] = []

    def healthy_nodes(self):
        with self._lock:
            return [n for n in self.nodes if n not in self._banned]

    def banned_nodes(self):
        with self._lock:
            return sorted(self._banned)

    def all_blocked(self):
        with self._lock:
            return bool(self.nodes) and not any(n not in self._banned for n in self.nodes)

    def mark_blocked(self, node):
        with self._lock:
            self._banned.add(node)
            self.banned_calls.append(node)

    def unblock(self, node):
        with self._lock:
            self._banned.discard(node)
            self._strikes.pop(node, None)
            self.unblock_calls.append(node)

    def record_no_email(self, node):
        with self._lock:
            count = self._strikes.get(node, 0) + 1
            self._strikes[node] = count
            return count

    def record_success(self, node):
        with self._lock:
            self._strikes.pop(node, None)

    def refresh_nodes(self):
        with self._lock:
            return list(self.nodes)


class _FakeRegistrar:
    """Records every ``_do_one`` invocation and lets tests steer outcomes."""

    def __init__(self, wave_fn=None, probe_fn=None):
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self._wave_fn = wave_fn or (
            lambda index, node: {"account_id": 100 + index, "email": f"a{index}@example.com"}
        )
        self._probe_fn = probe_fn or (
            lambda node, index: {"account_id": 900 + index, "email": f"p{index}@example.com"}
        )

    def __call__(
        self,
        index,
        *,
        forced_node=None,
        otp_timeout_seconds=None,
        retry_otp_once=True,
        report_no_email=True,
        worker_context=None,
        **kwargs,
    ):
        node = forced_node or "Node A"
        with self._lock:
            self.calls.append(
                {
                    "index": index,
                    "forced_node": forced_node,
                    "otp_timeout_seconds": otp_timeout_seconds,
                    "retry_otp_once": retry_otp_once,
                    "report_no_email": report_no_email,
                    "node": node,
                }
            )
            if worker_context is not None:
                worker_context["node"] = node
                worker_context["index"] = index
        if forced_node:
            return self._probe_fn(node, index)
        return self._wave_fn(index, node)

    def wave_calls(self):
        return [c for c in self.calls if not c["forced_node"]]

    def probe_calls(self):
        return [c for c in self.calls if c["forced_node"]]


def _run_pulse(registrar, allocator, *, count=1, logger=None, **config_overrides):
    config = PulseConfig(**config_overrides)
    logger = logger or _FakeLogger()
    controller = PulseRegistration(
        do_one=registrar,
        allocator=allocator,
        config=config,
        logger=logger,
        count=count,
    )
    controller.run()
    return logger, controller


# ----------------------------------------------------------- allocator ban state


def _make_allocator(slot_count=2, nodes=("Node A", "Node B")):
    client = MihomoClient(
        controller_url="http://mihomo.test:9090",
        proxy_url="http://mihomo.test:7890",
        group="REGISTER-ALL",
    )
    client.slot_count = slot_count

    def fake_request(method, path, **kwargs):
        if method == "GET" and path == "/proxies/REGISTER-ALL":
            return {"now": "Node A", "all": list(nodes)}
        if method == "GET" and path == "/proxies":
            return {
                "proxies": {name: {"type": "VLESS", "alive": True} for name in nodes}
            }
        if method == "PUT" and path.startswith("/proxies/REGISTER-SLOT-"):
            return {}
        raise AssertionError((method, path))

    client._request = fake_request
    return client.create_registration_allocator()


def test_allocator_excludes_banned_nodes_from_round_robin():
    allocator = _make_allocator()
    allocator.mark_blocked("Node A")
    assert allocator.healthy_nodes() == ["Node B"]
    assert not allocator.all_blocked()
    first = allocator.acquire()
    second = allocator.acquire()
    assert first.node == "Node B"
    assert second.node == "Node B"
    first.release()
    second.release()


def test_allocator_all_blocked_raises_on_acquire():
    allocator = _make_allocator()
    allocator.mark_blocked("Node A")
    allocator.mark_blocked("Node B")
    assert allocator.all_blocked()
    assert allocator.healthy_nodes() == []
    with pytest.raises(MihomoNodeError):
        allocator.acquire()


def test_allocator_acquire_node_pins_specific_node():
    allocator = _make_allocator()
    lease = allocator.acquire_node("Node B")
    assert lease.node == "Node B"
    # acquire_node takes the lowest free slot and pins it to the requested node
    assert lease.proxy == "http://mihomo.test:7901"
    lease.release()
    with pytest.raises(MihomoNodeError):
        allocator.acquire_node("Missing")


def test_allocator_strikes_reset_by_success_and_unblock():
    allocator = _make_allocator()
    assert allocator.record_no_email("Node A") == 1
    assert allocator.record_no_email("Node A") == 2
    allocator.record_success("Node A")
    assert allocator.record_no_email("Node A") == 1
    allocator.mark_blocked("Node A")
    allocator.unblock("Node A")
    assert allocator.banned_nodes() == []
    assert allocator.healthy_nodes() == ["Node A", "Node B"]


# ---------------------------------------------------------------- chatgpt OTP


def test_chatgpt_protocol_otp_timeout_override():
    from platforms.chatgpt.plugin import ChatGPTPlatform

    platform = ChatGPTPlatform(config=RegisterConfig(extra={"otp_timeout": 90}))
    adapter = platform.build_protocol_mailbox_adapter()
    assert adapter.otp_spec.timeout == 90


def test_chatgpt_protocol_otp_timeout_defaults_to_180():
    from platforms.chatgpt.plugin import ChatGPTPlatform

    platform = ChatGPTPlatform(config=RegisterConfig())
    adapter = platform.build_protocol_mailbox_adapter()
    assert adapter.otp_spec.timeout == 180


def test_otp_callback_reports_when_code_was_actually_received():
    state = {"received": False}

    class Mailbox:
        def wait_for_code(self, _account, **_kwargs):
            return "123456"

    ctx = SimpleNamespace(
        platform=SimpleNamespace(
            mailbox=Mailbox(),
            is_cancel_requested=lambda: False,
        ),
        identity=SimpleNamespace(mailbox_account=object(), before_ids=set()),
        extra={
            "_otp_received_callback": lambda: state.__setitem__("received", True),
        },
        log=lambda _message: None,
    )

    callback = build_otp_callback(ctx)

    assert callback is not None
    assert callback() == "123456"
    assert state["received"] is True


# -------------------------------------------------------------- pulse controller


def test_pulse_success_counts_accounts_and_sets_result():
    allocator = _FakeAllocator()
    registrar = _FakeRegistrar()
    logger, _ = _run_pulse(registrar, allocator, count=4)
    assert logger.finished == (TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success"] == 4
    assert logger.result_data["fail"] == 0
    assert logger.result_data["pulse"] is True
    assert logger.result_data["unlimited"] is False
    assert logger.finish_calls == [(TASK_STATUS_SUCCEEDED, "")]
    # one wave = every healthy node, so two waves of two accounts each
    assert sorted(c["index"] for c in registrar.wave_calls()) == [0, 1, 2, 3]


def test_pulse_honors_requested_wave_concurrency():
    allocator = _FakeAllocator(
        nodes=("Node A", "Node B", "Node C", "Node D", "Node E"),
        slot_count=5,
    )
    state = {"active": 0, "maximum": 0}
    lock = threading.Lock()

    def wave_fn(index, node):
        with lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        try:
            time.sleep(0.02)
            return {"account_id": 100 + index, "email": f"a{index}@example.com"}
        finally:
            with lock:
                state["active"] -= 1

    registrar = _FakeRegistrar(wave_fn=wave_fn)
    logger, _ = _run_pulse(
        registrar,
        allocator,
        count=5,
        wave_concurrency=2,
    )

    assert logger.finished == (TASK_STATUS_SUCCEEDED, "")
    assert state["maximum"] == 2


def test_pulse_updates_progress_before_the_whole_wave_finishes():
    allocator = _FakeAllocator(nodes=("Node A", "Node B"), slot_count=2)
    logger = _FakeLogger()
    slow_release = threading.Event()
    fast_finished = threading.Event()

    def wave_fn(index, node):
        if index == 1:
            slow_release.wait(timeout=2)
        else:
            fast_finished.set()
        return {"account_id": 100 + index, "email": f"a{index}@example.com"}

    registrar = _FakeRegistrar(wave_fn=wave_fn)
    controller = PulseRegistration(
        do_one=registrar,
        allocator=allocator,
        config=PulseConfig(wave_concurrency=2),
        logger=logger,
        count=2,
    )
    thread = threading.Thread(target=controller.run)
    thread.start()
    try:
        assert fast_finished.wait(timeout=1)
        deadline = time.monotonic() + 1
        saw_partial_progress = False
        while time.monotonic() < deadline:
            with logger._lock:
                saw_partial_progress = ("progress", 1, 2) in logger.events
            if saw_partial_progress:
                break
            time.sleep(0.01)
        assert saw_partial_progress
    finally:
        slow_release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert logger.finished == (TASK_STATUS_SUCCEEDED, "")


def test_pulse_wave_timeout_fails_instead_of_waiting_forever():
    allocator = _FakeAllocator(nodes=("Node A",), slot_count=1)
    logger = _FakeLogger()
    release = threading.Event()
    registrar = _FakeRegistrar(
        wave_fn=lambda index, node: (
            release.wait(timeout=2)
            or {"account_id": 100 + index, "email": f"a{index}@example.com"}
        )
    )
    controller = PulseRegistration(
        do_one=registrar,
        allocator=allocator,
        config=PulseConfig(wave_timeout_seconds=0.05),
        logger=logger,
        count=1,
    )
    thread = threading.Thread(target=controller.run)
    thread.start()
    thread.join(timeout=1)
    try:
        assert not thread.is_alive()
        assert logger.finished[0] == TASK_STATUS_FAILED
        assert "注册波次超过" in logger.finished[1]
    finally:
        release.set()


def test_pulse_no_email_requeues_bans_and_probe_recovers():
    allocator = _FakeAllocator(nodes=("Node A",))
    attempts = {"count": 0}

    def wave_fn(index, node):
        attempts["count"] += 1
        if attempts["count"] <= 1:
            return _NO_EMAIL_MARKER
        return {"account_id": 100 + index, "email": f"a{index}@example.com"}

    registrar = _FakeRegistrar(
        wave_fn=wave_fn,
        probe_fn=lambda node, index: {"account_id": 900 + index, "email": f"p{index}@example.com"},
    )
    logger, _ = _run_pulse(
        registrar,
        allocator,
        count=1,
        probe_interval_seconds=3600,
        ban_after_consecutive_no_email=1,
    )

    assert allocator.banned_calls == ["Node A"]
    assert allocator.unblock_calls == ["Node A"]
    assert logger.finished == (TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success"] == 1
    assert logger.result_data["fail"] == 0
    # The first no-email result pauses the node; its probe consumes the retry.
    assert [c["index"] for c in registrar.calls] == [0, 0]


def test_pulse_cloudflare_failure_bans_node_outright():
    allocator = _FakeAllocator(nodes=("Node A", "Node B"))
    registrar = _FakeRegistrar(
        wave_fn=lambda index, node: (
            "注册失败: Cloudflare challenge during ChatGPT homepage (HTTP 403)"
        )
    )
    logger, _ = _run_pulse(registrar, allocator, count=1)

    # A CF challenge means the node's egress IP is flagged; it is banned
    # immediately, without waiting for a no-email strike.
    assert "Node A" in allocator.banned_calls
    assert allocator.banned_nodes() == ["Node A"]
    assert logger.result_data["fail"] == 1
    assert logger.result_data["success"] == 0


def test_pulse_regular_failure_does_not_ban_node():
    allocator = _FakeAllocator(nodes=("Node A", "Node B"))
    registrar = _FakeRegistrar(
        wave_fn=lambda index, node: (
            "注册失败: 邮箱验证码校验失败: wrong_email_otp_code: Wrong code. "
            "Please check it and try again."
        )
    )
    logger, _ = _run_pulse(registrar, allocator, count=1)

    # wrong OTP / already-exists are account-level failures, not IP bans.
    assert allocator.banned_nodes() == []


def test_pulse_stops_when_the_shared_browser_event_loop_is_unresponsive():
    allocator = _FakeAllocator(nodes=("Node A",))
    registrar = _FakeRegistrar(
        wave_fn=lambda index, node: "共享浏览器事件循环超过 690 秒未响应"
    )
    logger, _ = _run_pulse(registrar, allocator, count=0)

    assert logger.finished[0] == TASK_STATUS_FAILED
    assert "共享浏览器池失去响应" in logger.finished[1]
    assert len(registrar.wave_calls()) == 1
    assert allocator.banned_calls == []
    assert logger.result_data["fail"] == 1
    assert logger.result_data["success"] == 0


def test_pulse_pool_exhausted_stops_the_task():
    allocator = _FakeAllocator(nodes=("Node A", "Node B"))
    registrar = _FakeRegistrar(wave_fn=lambda index, node: _POOL_EXHAUSTED_MARKER)
    logger, _ = _run_pulse(registrar, allocator, count=100)

    # Pool exhaustion is fatal: the task finishes FAILED instead of looping on
    # a pool-exhaustion failure per worker for the rest of the run.
    assert logger.finished == (TASK_STATUS_FAILED, "本地微软邮箱池已用尽，注册任务终止")
    assert logger.result_data["success"] == 0
    assert logger.result_data["fail"] == 0
    # Only the first wave (2 workers) ran; no further indices were consumed.
    assert len(registrar.wave_calls()) == 2


def test_unlimited_registration_stops_on_pool_exhaustion():
    calls = {"n": 0}

    def do_one(index):
        calls["n"] += 1
        return _POOL_EXHAUSTED_MARKER

    logger = _FakeLogger()
    tasks_module._run_unlimited_registration(do_one, concurrency=4, logger=logger)

    assert logger.finished == (TASK_STATUS_FAILED, "本地微软邮箱池已用尽，注册任务终止")
    # Only the initial 4 workers ran; nothing further was submitted.
    assert calls["n"] == 4


def test_probe_forces_banned_node_and_short_otp_timeout():
    allocator = _FakeAllocator(nodes=("Node A", "Node B"))
    allocator.mark_blocked("Node A")
    allocator.mark_blocked("Node B")
    registrar = _FakeRegistrar()
    logger, _ = _run_pulse(
        registrar,
        allocator,
        count=1,
        probe_otp_timeout_seconds=90,
        probe_interval_seconds=3600,
    )

    probes = registrar.probe_calls()
    assert probes, "expected the probe to run when all nodes are banned"
    call = probes[0]
    assert call["forced_node"] in {"Node A", "Node B"}
    assert call["otp_timeout_seconds"] == 90
    assert call["retry_otp_once"] is False
    assert call["report_no_email"] is False
    # the successful probe consumed the account; only the probed node unbanned
    assert logger.finished == (TASK_STATUS_SUCCEEDED, "")
    assert logger.result_data["success"] == 1
    assert len(allocator.unblock_calls) == 1


def test_probe_no_email_keeps_node_banned():
    allocator = _FakeAllocator(nodes=("Node A",))
    allocator.mark_blocked("Node A")

    def probe_fn(node, index):
        return _NO_EMAIL_MARKER

    registrar = _FakeRegistrar(probe_fn=probe_fn, wave_fn=lambda i, n: _NO_EMAIL_MARKER)
    logger = _FakeLogger()

    def stop_after():
        for _ in range(3):
            if logger.cancel_requested:
                return
            logger.cancel_requested = True

    # The task can never succeed while the only node stays banned; cancel it.
    timer = threading.Timer(0.5, stop_after)
    timer.start()
    try:
        _run_pulse(registrar, allocator, count=1, logger=logger, probe_interval_seconds=60)
    finally:
        timer.cancel()

    assert allocator.unblock_calls == []
    assert logger.finished == (TASK_STATUS_CANCELLED, "任务已取消")


def test_probe_pre_otp_error_keeps_node_banned_and_requeues():
    allocator = _FakeAllocator(nodes=("Node A",))
    allocator.mark_blocked("Node A")
    registrar = _FakeRegistrar(
        probe_fn=lambda node, index: "Cloudflare challenge during homepage",
    )
    logger = _FakeLogger()
    controller = PulseRegistration(
        do_one=registrar,
        allocator=allocator,
        config=PulseConfig(),
        logger=logger,
        count=1,
    )

    assert controller._pop_one_index() == 0
    controller._run_one_probe("Node A", 0)

    assert allocator.unblock_calls == []
    assert allocator.banned_nodes() == ["Node A"]
    assert controller._pop_one_index() == 0


def test_probe_post_otp_error_unbans_and_requeues():
    allocator = _FakeAllocator(nodes=("Node A",))
    allocator.mark_blocked("Node A")

    def registrar(index, *, forced_node=None, worker_context=None, **_kwargs):
        assert forced_node == "Node A"
        assert worker_context is not None
        worker_context["node"] = forced_node
        worker_context["otp_received"] = True
        return "注册账号 401 验活失败：token 已失效"

    controller = PulseRegistration(
        do_one=registrar,
        allocator=allocator,
        config=PulseConfig(),
        logger=_FakeLogger(),
        count=1,
    )

    assert controller._pop_one_index() == 0
    controller._run_one_probe("Node A", 0)

    assert allocator.unblock_calls == ["Node A"]
    assert controller._pop_one_index() == 0


def test_probe_cycle_covers_every_currently_banned_node():
    allocator = _FakeAllocator(nodes=("Node A", "Node B", "Node C"))
    for node in allocator.nodes:
        allocator.mark_blocked(node)
    registrar = _FakeRegistrar()
    controller = PulseRegistration(
        do_one=registrar,
        allocator=allocator,
        config=PulseConfig(),
        logger=_FakeLogger(),
        count=3,
    )

    controller._run_probe_cycle()

    assert {call["forced_node"] for call in registrar.probe_calls()} == set(allocator.nodes)
    assert set(allocator.unblock_calls) == set(allocator.nodes)


def test_pulse_unlimited_fires_until_cancel():
    allocator = _FakeAllocator(nodes=("Node A",))
    logger = _FakeLogger()
    state = {"n": 0}

    def wave_fn(index, node):
        state["n"] += 1
        if state["n"] >= 5:
            logger.cancel_requested = True
        return {"account_id": 100 + index, "email": f"a{index}@example.com"}

    registrar = _FakeRegistrar(wave_fn=wave_fn)
    _run_pulse(registrar, allocator, count=0, logger=logger)

    assert logger.finished == (TASK_STATUS_CANCELLED, "任务已取消")
    assert logger.result_data["unlimited"] is True
    assert logger.result_data["success"] >= 5


def test_pulse_config_from_payload_and_clamping():
    cfg = PulseConfig.from_payload({})
    assert cfg.pulse_interval_seconds == 0
    assert cfg.probe_interval_seconds == 600
    assert cfg.probe_otp_timeout_seconds == 90
    assert cfg.ban_after_consecutive_no_email == 3
    assert cfg.wave_concurrency == MAX_REGISTER_CONCURRENCY
    assert cfg.wave_timeout_seconds == 900

    cfg2 = PulseConfig.from_payload(
        {
            "probe_interval_seconds": 60,
            "probe_otp_timeout_seconds": 45,
            "ban_after_consecutive_no_email": 3,
            "concurrency": 24,
            "pulse_wave_timeout_seconds": 1200,
        }
    )
    assert cfg2.probe_interval_seconds == 60
    assert cfg2.probe_otp_timeout_seconds == 45
    assert cfg2.ban_after_consecutive_no_email == 3
    assert cfg2.wave_concurrency == 24
    assert cfg2.wave_timeout_seconds == 1200


# -------------------------------------------------------------- task wiring


class _WiringLogger:
    def __init__(self, task_id="pulse-wiring"):
        self.task_id = task_id
        self.finished = None
        self.messages = []

    def set_progress(self, *_args):
        pass

    def log(self, message, **_kwargs):
        self.messages.append(message)

    def finish(self, status, *, error=""):
        self.finished = (status, error)

    def is_cancel_requested(self):
        return False

    def set_result_data(self, _data):
        pass


def test_execute_register_task_defaults_to_pulse_for_proxy_node(monkeypatch):
    pulse_calls = []
    unlimited_calls = []

    class Allocator:
        node_count = 2
        slot_count = 50

    monkeypatch.setattr(
        "core.mihomo_client.mihomo_client.create_registration_allocator",
        lambda preferred_node=None, **_kwargs: Allocator(),
    )
    monkeypatch.setattr(tasks_module, "get", lambda _platform: object)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *a, **kw: object())
    monkeypatch.setattr(
        tasks_module,
        "_run_pulse_registration",
        lambda *args, **kwargs: pulse_calls.append(kwargs),
    )
    monkeypatch.setattr(
        tasks_module,
        "_run_unlimited_registration",
        lambda *args, **kwargs: unlimited_calls.append(kwargs),
    )

    logger = _WiringLogger()
    tasks_module._execute_register_task(
        {
            "count": 0,
            "concurrency": 1,
            "proxy_node": "Node A",
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert len(pulse_calls) == 1
    assert pulse_calls[0]["count"] == 0
    assert unlimited_calls == []


def test_execute_register_task_pulse_false_falls_back_to_continuous(monkeypatch):
    pulse_calls = []
    unlimited_calls = []

    class Allocator:
        node_count = 2
        slot_count = 50

    monkeypatch.setattr(
        "core.mihomo_client.mihomo_client.create_registration_allocator",
        lambda preferred_node=None, **_kwargs: Allocator(),
    )
    monkeypatch.setattr(tasks_module, "get", lambda _platform: object)
    monkeypatch.setattr("core.base_mailbox.create_mailbox", lambda *a, **kw: object())
    monkeypatch.setattr(
        tasks_module,
        "_run_pulse_registration",
        lambda *args, **kwargs: pulse_calls.append(kwargs),
    )
    monkeypatch.setattr(
        tasks_module,
        "_run_unlimited_registration",
        lambda *args, **kwargs: unlimited_calls.append(kwargs),
    )

    logger = _WiringLogger()
    tasks_module._execute_register_task(
        {
            "count": 0,
            "concurrency": 1,
            "proxy_node": "Node A",
            "pulse": False,
            "extra": {"identity_provider": "mailbox"},
        },
        logger,
    )

    assert pulse_calls == []
    assert len(unlimited_calls) == 1


# -------------------------------------------------------------------- API layer


def test_register_api_accepts_pulse_fields(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "api.task_commands.mihomo_client.validate_node",
        lambda name: {"name": name, "alive": True},
    )
    monkeypatch.setattr(
        "api.task_commands.command_service.create_register_task",
        lambda payload: captured.update(payload) or {"task_id": "task_pulse"},
    )

    response = client.post(
        "/api/tasks/register",
        json={
            "count": 1,
            "concurrency": 5,
            "proxy_node": "US Fast",
            "pulse": True,
            "pulse_interval_seconds": 0,
            "probe_interval_seconds": 900,
            "probe_otp_timeout_seconds": 120,
            "ban_after_consecutive_no_email": 3,
            "extra": {"mail_provider": "domain_inbucket"},
        },
    )

    assert response.status_code == 200
    assert captured["pulse"] is True
    assert captured["probe_interval_seconds"] == 900
    assert captured["probe_otp_timeout_seconds"] == 120
    assert captured["ban_after_consecutive_no_email"] == 3


def test_register_api_rejects_probe_fields_without_node(client):
    response = client.post(
        "/api/tasks/register",
        json={
            "count": 1,
            "probe_interval_seconds": 900,
            "extra": {"mail_provider": "domain_inbucket"},
        },
    )

    assert response.status_code == 400
    assert "脉冲/探测参数" in response.json()["detail"]
