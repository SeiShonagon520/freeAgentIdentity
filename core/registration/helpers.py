from __future__ import annotations

import time
from typing import Any

from .errors import BrowserReuseRequiredError, IdentityResolutionError, RegistrationUnsupportedError
from .models import RegistrationContext


def has_reusable_oauth_browser(identity: Any) -> bool:
    return bool((getattr(identity, "chrome_user_data_dir", "") or "").strip() or (getattr(identity, "chrome_cdp_url", "") or "").strip())


def resolve_timeout(extra: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        value = extra.get(key)
        if value not in (None, ""):
            return int(value)
    return int(default)


def ensure_identity_email(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "email", ""):
        raise IdentityResolutionError(message)


def ensure_mailbox_identity(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "has_mailbox", False):
        raise IdentityResolutionError(message)


def ensure_oauth_executor_allowed(ctx: RegistrationContext, allowed_executor_types: tuple[str, ...] | None, message: str | None = None) -> None:
    if not allowed_executor_types:
        return
    if ctx.executor_type not in allowed_executor_types:
        expected = ", ".join(allowed_executor_types)
        raise RegistrationUnsupportedError(message or f"{ctx.platform_display_name} 当前 OAuth 仅支持 executor_type={expected}")


def ensure_oauth_browser_reuse(ctx: RegistrationContext, message: str) -> None:
    if not has_reusable_oauth_browser(ctx.identity):
        raise BrowserReuseRequiredError(message)


def build_otp_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    code_pattern: str | None = None,
    wait_message: str = "等待验证码...",
    success_label: str = "验证码",
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    def otp_cb():
        ctx.log(wait_message)
        total_timeout = int(timeout if timeout is not None else 120)
        deadline = time.monotonic() + max(total_timeout, 1)
        while True:
            if ctx.platform.is_cancel_requested():
                raise RuntimeError("任务已取消")
            remaining = max(1, int(deadline - time.monotonic()))
            kwargs = {
                "keyword": keyword,
                "before_ids": getattr(ctx.identity, "before_ids", set()),
                # Poll in short windows so stopping a task takes seconds rather
                # than waiting for the full mailbox timeout.
                "timeout": min(remaining, 5),
            }
            if code_pattern:
                kwargs["code_pattern"] = code_pattern
            try:
                code = mailbox.wait_for_code(mail_acct, **kwargs)
            except TimeoutError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"等待验证码超时 ({total_timeout}s)"
                    ) from exc
                continue
            if code:
                otp_received_callback = ctx.extra.get("_otp_received_callback")
                if callable(otp_received_callback):
                    otp_received_callback()
                ctx.log(f"{success_label}: {code}")
            return code

    return otp_cb


def build_link_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    wait_message: str = "等待验证链接邮件...",
    success_label: str = "验证链接",
    preview_chars: int = 80,
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    def link_cb():
        ctx.log(wait_message)
        before_ids = mailbox.get_current_ids(mail_acct)
        kwargs = {"keyword": keyword, "before_ids": before_ids}
        if timeout is not None:
            kwargs["timeout"] = timeout
        link = mailbox.wait_for_link(mail_acct, **kwargs)
        if link:
            preview = link if len(link) <= preview_chars else f"{link[:preview_chars]}..."
            ctx.log(f"{success_label}: {preview}")
        return link

    return link_cb
