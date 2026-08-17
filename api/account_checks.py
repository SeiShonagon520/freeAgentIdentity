from __future__ import annotations

from fastapi import APIRouter, HTTPException
from typing import Literal

from pydantic import BaseModel, Field

from application.account_checks import AccountChecksService
from core.mihomo_client import MihomoNodeError, MihomoUnavailableError, mihomo_client

router = APIRouter(prefix="/accounts", tags=["account-checks"])
service = AccountChecksService()


class RefreshTokenCheckRequest(BaseModel):
    platform: Literal["chatgpt"] = "chatgpt"
    concurrency: int = Field(default=100, ge=1, le=200)
    proxy_node: str | None = None
    # Browser-based verification: run the AT check through a real camoufox
    # page (fetch) instead of the protocol client, avoiding Cloudflare 403.
    browser: bool = False


@router.post("/check-refresh-tokens")
def check_refresh_tokens(body: RefreshTokenCheckRequest):
    proxy_node = str(body.proxy_node or "").strip()
    if proxy_node:
        try:
            mihomo_client.validate_node(proxy_node)
        except MihomoNodeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except MihomoUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
    return service.check_refresh_tokens_async(
        body.platform,
        body.concurrency,
        proxy_node=proxy_node or None,
        browser=body.browser,
    )
