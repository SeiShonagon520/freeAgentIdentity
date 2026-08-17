"""当前注册页面使用的邮箱抽象与工厂。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import html
import logging
import re

logger = logging.getLogger(__name__)


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict | None = None


class BaseMailbox(ABC):
    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱。"""

    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        """等待并返回验证码。"""

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合，用于过滤旧邮件。"""

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        raise NotImplementedError(f"{self.__class__.__name__} 暂不支持 wait_for_link()")


class FallbackMailbox(BaseMailbox):
    """默认邮箱不可用时，依次尝试其他已启用邮箱。"""

    def __init__(self, providers: list[tuple[str, BaseMailbox]]):
        self.providers = providers
        self._accounts: dict[str, BaseMailbox] = {}

    def _resolve(self, account: MailboxAccount) -> BaseMailbox:
        provider_key = str((account.extra or {}).get("mailbox_provider_key") or "")
        for key, mailbox in self.providers:
            if key == provider_key:
                return mailbox
        mailbox = self._accounts.get(account.email)
        if mailbox is None:
            raise RuntimeError(f"未找到邮箱 provider 上下文: {account.email}")
        return mailbox

    def get_email(self) -> MailboxAccount:
        errors: list[str] = []
        for key, mailbox in self.providers:
            try:
                account = mailbox.get_email()
                account.extra = dict(account.extra or {})
                account.extra["mailbox_provider_key"] = key
                self._accounts[account.email] = mailbox
                return account
            except Exception as exc:
                errors.append(f"{key}: {exc}")
        raise RuntimeError("所有邮箱 provider 均创建失败: " + " | ".join(errors))

    def test_connection(self) -> None:
        """Remove unavailable providers before concurrent workers start."""
        available: list[tuple[str, BaseMailbox]] = []
        errors: list[str] = []
        for key, mailbox in self.providers:
            tester = getattr(mailbox, "test_connection", None)
            if not callable(tester):
                available.append((key, mailbox))
                continue
            try:
                tester()
            except Exception as exc:
                errors.append(f"{key}: {exc}")
            else:
                available.append((key, mailbox))
        self.providers = available
        if not available:
            raise RuntimeError("所有邮箱 provider 连接失败: " + " | ".join(errors))

    def get_current_ids(self, account: MailboxAccount) -> set:
        return self._resolve(account).get_current_ids(account)

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        return self._resolve(account).wait_for_code(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            code_pattern=code_pattern,
        )

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        return self._resolve(account).wait_for_link(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
        )


def _extract_verification_link(text: str, keyword: str = "") -> str | None:
    combined = str(text or "")
    if keyword and keyword.lower() not in combined.lower():
        return None
    urls = [
        html.unescape(raw).rstrip(").,;")
        for raw in re.findall(r'https?://[^\s<>"\']+', combined, re.IGNORECASE)
    ]
    return urls[0] if urls else None


def _create_local_ms_pool(extra: dict, proxy: str | None) -> BaseMailbox:
    from core.local_ms_mailbox import LocalMicrosoftMailboxPool

    return LocalMicrosoftMailboxPool(
        pool_text=extra.get("local_ms_pool_text", ""),
        pool_file=extra.get("local_ms_pool_file", ""),
        state_file=extra.get("local_ms_pool_state_file", ""),
        graph_scope=extra.get("local_ms_graph_scope", ""),
        allow_reuse=str(extra.get("local_ms_pool_allow_reuse", "")).strip().lower()
        in {"1", "true", "yes", "on"},
        alias_count=extra.get("local_ms_pool_alias_count", 6),
        proxy=proxy,
    )


def _create_api_mailbox(extra: dict, proxy: str | None) -> BaseMailbox:
    from core.api_mailbox import ApiMailboxPool

    return ApiMailboxPool(
        pool_text=extra.get("api_mailbox_pool_text", ""),
        state_file=extra.get("api_mailbox_state_file", ""),
        allow_reuse=str(extra.get("api_mailbox_allow_reuse", "")).strip().lower()
        in {"1", "true", "yes", "on"},
        poll_interval=extra.get("api_mailbox_poll_interval", 3),
        request_timeout=extra.get("api_mailbox_request_timeout", 15),
        proxy=proxy,
    )


def _create_domain_imap_catchall(extra: dict, proxy: str | None) -> BaseMailbox:
    del proxy  # IMAP is a direct connection; proxy routing is not supported by imaplib.
    from core.domain_imap_mailbox import DomainImapCatchallMailbox

    return DomainImapCatchallMailbox.from_config(extra)


def _create_domain_inbucket(extra: dict, proxy: str | None) -> BaseMailbox:
    del proxy
    from core.inbucket_domain_mailbox import InbucketDomainMailbox

    return InbucketDomainMailbox.from_config(extra)


MAILBOX_FACTORY_REGISTRY = {
    "local_ms_pool": _create_local_ms_pool,
    "api_mailbox": _create_api_mailbox,
    "domain_imap_catchall": _create_domain_imap_catchall,
    "domain_inbucket": _create_domain_inbucket,
}


def create_mailbox(provider: str, extra: dict | None = None, proxy: str | None = None) -> BaseMailbox:
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    provider_key = str(provider or "").strip()
    if not provider_key:
        raise RuntimeError("未选择邮箱服务，请在注册弹窗中手动选择")

    definitions = ProviderDefinitionsRepository()
    settings = ProviderSettingsRepository()
    definition = definitions.get_by_key("mailbox", provider_key)
    if not definition or not definition.enabled:
        raise RuntimeError(f"邮箱 provider 不存在、未启用或不受支持: {provider_key}")
    factory = MAILBOX_FACTORY_REGISTRY.get(definition.driver_type or provider_key)
    if factory is None:
        raise RuntimeError(f"邮箱 provider 不存在、未启用或不受支持: {provider_key}")
    resolved = settings.resolve_runtime_settings("mailbox", provider_key, dict(extra or {}))
    return factory(resolved, proxy)
