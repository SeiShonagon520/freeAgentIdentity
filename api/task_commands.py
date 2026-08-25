from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.task_commands import TaskCommandsService
from application.tasks_query import TasksQueryService
from core.mihomo_client import MihomoNodeError, MihomoUnavailableError, mihomo_client
from core.runtime_mode import har_capture_available


router = APIRouter(prefix="/tasks", tags=["task-commands"])
command_service = TaskCommandsService()
query_service = TasksQueryService()


class RegisterTaskRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    # A value of zero creates an unlimited task that runs until cancelled.
    count: int = Field(default=1, ge=0)
    concurrency: int = Field(default=1, ge=1, le=50)
    proxy: Optional[str] = None
    proxy_node: Optional[str] = None
    # Use the full Mihomo pool without pinning the first worker to a named node.
    proxy_pool: bool = False
    # For the Microsoft mailbox pool: split each parent mailbox into six one-time
    # child addresses (+reg1..+reg6).  When false, register directly with the
    # parent mailbox address itself (each mailbox used once).
    split_register: bool = True
    # Rotating-residential-proxy extract API: each worker fetches a fresh IP
    # from this URL and swaps to a new one on Cloudflare challenges.
    proxy_api_url: Optional[str] = None
    executor_type: Literal["protocol", "headless", "headed"] = "protocol"
    extra: dict = Field(default_factory=dict)
    # Manual camoufox HAR-capture mode: open a real browser, operator registers
    # by hand, HAR saved for later registration-template extraction.
    har_capture: bool = False
    # HAR-capture for 2FA binding: capture from registration through binding
    # TOTP 2FA in the OpenAI security settings.  When set, the browser does NOT
    # auto-open Codex OAuth after registration (the operator navigates to the
    # security settings and binds 2FA manually).
    har_capture_2fa: bool = False
    har_path: Optional[str] = None
    # Pulse registration: fire all healthy Mihomo nodes concurrently per wave
    # (bounded only by the slot pool) and pause a node whose IP stops receiving
    # OAI verification mail, probing it on a schedule until OAI accepts the IP
    # again.  Only meaningful when a ``proxy_node`` is selected; the backend
    # falls back to the continuous path otherwise.
    pulse: bool = True
    pulse_interval_seconds: float = Field(default=0, ge=0, le=3600)
    probe_interval_seconds: float = Field(default=600, ge=30, le=86400)
    probe_otp_timeout_seconds: int = Field(default=90, ge=20, le=3600)
    ban_after_consecutive_no_email: int = Field(default=3, ge=1, le=10)
    # How many banned nodes a single probe cycle may test (rotating).  Kept
    # small so banned IPs are not flooded with registration emails every cycle.
    probe_batch_size: int = Field(default=5, ge=1, le=20)


@router.post("/register")
def create_register_task(body: RegisterTaskRequest):
    payload = body.model_dump()
    proxy_node = str(body.proxy_node or "").strip()
    proxy_pool = bool(body.proxy_pool)
    proxy_api_url = str(body.proxy_api_url or "").strip() or None
    if body.har_capture and not har_capture_available():
        raise HTTPException(400, "服务器模式和公开版均不支持 HAR 抓包")
    if proxy_node and str(body.proxy or "").strip():
        raise HTTPException(400, "代理地址和 Mihomo 节点不能同时选择")
    if proxy_api_url and (proxy_node or str(body.proxy or "").strip()):
        raise HTTPException(
            400,
            "动态 IP API 和 Mihomo 节点/静态代理不能同时选择",
        )
    if proxy_pool and (proxy_node or proxy_api_url or str(body.proxy or "").strip()):
        raise HTTPException(400, "Mihomo 代理池不能和其他代理模式同时选择")
    if proxy_node or proxy_pool:
        try:
            if proxy_node:
                mihomo_client.validate_node(proxy_node)
            else:
                available_nodes = [
                    item
                    for item in mihomo_client.list_nodes().get("nodes", [])
                    if item.get("alive") is not False
                    and mihomo_client.is_node_enabled(str(item.get("name") or ""))
                ]
                if not available_nodes:
                    raise MihomoNodeError("Mihomo 代理池没有可用节点")
        except MihomoNodeError as exc:
            raise HTTPException(400, str(exc))
        except MihomoUnavailableError as exc:
            raise HTTPException(503, str(exc))
    payload["proxy_node"] = proxy_node or None
    payload["proxy_pool"] = proxy_pool
    payload["proxy_api_url"] = proxy_api_url
    # Dynamic IP mode has no Mihomo node pool to pulse/ban/probe; force pulse off.
    if proxy_api_url:
        payload["pulse"] = False
    # Pulse parameters only apply to a Mihomo-node task.  Tolerate the default
    # ``pulse=True`` without a node (the backend falls back to the continuous
    # path), but reject an explicitly-tuned probe config that has nowhere to go.
    probe_fields_explicit = (
        body.pulse_interval_seconds != 0
        or body.probe_interval_seconds != 600
        or body.probe_otp_timeout_seconds != 90
        or body.ban_after_consecutive_no_email != 3
        or body.probe_batch_size != 5
    )
    if body.pulse and not proxy_node and not proxy_pool and not proxy_api_url and probe_fields_explicit:
        raise HTTPException(
            400,
            "脉冲/探测参数需要先选择一个 Mihomo 注册代理节点",
        )
    extra = dict(body.extra or {})
    extra["identity_provider"] = "mailbox"

    mail_provider = str(extra.get("mail_provider") or "").strip()
    if not mail_provider:
        raise HTTPException(400, "请选择本次注册使用的邮箱服务")

    extra["mail_provider"] = mail_provider
    if mail_provider == "local_ms_pool":
        # When split registration is on, each parent mailbox yields six one-time
        # child addresses (+reg1..+reg6).  When off, register with the parent
        # address itself so every mailbox is used exactly once.
        extra["local_ms_pool_alias_count"] = 6 if bool(body.split_register) else 1
        extra["local_ms_pool_allow_reuse"] = "false"
    else:
        extra.pop("local_ms_pool_alias_count", None)
    payload["extra"] = extra
    return command_service.create_register_task(payload)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    task = command_service.cancel_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0, tail: int = 300):
    if not query_service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return StreamingResponse(
        command_service.stream_task_events(task_id, since=since, tail=tail),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
