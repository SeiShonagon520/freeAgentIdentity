from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from core.db import ProviderDefinitionModel, ProviderSettingModel, engine

logger = logging.getLogger(__name__)

SUPPORTED_MAILBOX_PROVIDER_KEYS = ("local_ms_pool", "api_mailbox", "domain_imap_catchall", "domain_inbucket")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_BUILTIN_DEFINITIONS: list[dict] = [
    # ── mailbox ──────────────────────────────────────────────────────
    {
        "provider_type": "mailbox",
        "provider_key": "local_ms_pool",
        "label": "本地微软邮箱池",
        "description": "导入 Hotmail/Outlook 邮箱池，账号独立入库并按每个邮箱最多 6 次原子分配，优先通过 Microsoft Graph 收验证码",
        "driver_type": "local_ms_pool",
        "default_auth_mode": "pool",
        "enabled": True,
        "category": "custom",
        "auth_modes": [{"value": "pool", "label": "账号池"}],
        "fields": [
            {
                "key": "local_ms_graph_scope",
                "label": "Graph Scope",
                "placeholder": "https://graph.microsoft.com/Mail.Read offline_access",
                "category": "connection",
            },
        ],
    },
    {
        "provider_type": "mailbox",
        "provider_key": "api_mailbox",
        "label": "API 邮箱",
        "description": "使用固定邮箱及其专属 API 地址轮询获取验证码，支持一行一个邮箱----API URL",
        "driver_type": "api_mailbox",
        "default_auth_mode": "pool",
        "enabled": True,
        "category": "custom",
        "auth_modes": [{"value": "pool", "label": "邮箱 API 池"}],
        "fields": [
            {
                "key": "api_mailbox_pool_text",
                "label": "邮箱 API 池",
                "type": "textarea",
                "secret": True,
                "category": "auth",
                "placeholder": "user@example.com----https://mail.example.com/api/code?email=...&token=...",
                "hint": "每行一组，格式：邮箱----完整 API URL。URL 中的邮箱、密码、Token 等参数请保持原样。",
            },
            {
                "key": "api_mailbox_poll_interval",
                "label": "轮询间隔秒",
                "placeholder": "3",
                "default_value": "3",
                "category": "connection",
            },
            {
                "key": "api_mailbox_request_timeout",
                "label": "单次请求超时秒",
                "placeholder": "15",
                "default_value": "15",
                "category": "connection",
            },
            {
                "key": "api_mailbox_state_file",
                "label": "占用状态文件",
                "placeholder": "默认 data/.api_mailbox_pool_state.json",
                "category": "connection",
                "hint": "用于避免同一个邮箱被重复分配；删除该文件可重置占用状态。",
            },
            {
                "key": "api_mailbox_allow_reuse",
                "label": "允许重复使用邮箱",
                "type": "toggle",
                "category": "connection",
                "hint": "测试时可开启；批量注册建议关闭。",
            },
        ],
    },
    {
        "provider_type": "mailbox",
        "provider_key": "domain_imap_catchall",
        "label": "自有域名邮箱（IMAP 全收）",
        "description": "为每次注册生成一个 @域名 地址，并从同一个全收 IMAP 收件箱读取验证码。请先在邮局配置全收/通配别名。",
        "driver_type": "domain_imap_catchall",
        "default_auth_mode": "imap",
        "enabled": True,
        "category": "custom",
        "auth_modes": [{"value": "imap", "label": "IMAP 全收邮箱"}],
        "fields": [
            {
                "key": "domain_imap_domain",
                "label": "邮箱域名",
                "placeholder": "abaifly.edu.kg",
                "category": "connection",
                "hint": "必须是您已控制并已配置全收（catch-all）的域名；每次注册会自动生成新的地址。",
            },
            {
                "key": "domain_imap_host",
                "label": "IMAP 服务器",
                "placeholder": "mail.abaifly.edu.kg",
                "category": "connection",
                "hint": "请填写与 TLS 证书匹配的主机名。若直接填 IP，SSL 证书校验通常会失败。",
            },
            {
                "key": "domain_imap_port",
                "label": "IMAP 端口",
                "placeholder": "993",
                "default_value": "993",
                "category": "connection",
            },
            {
                "key": "domain_imap_security",
                "label": "IMAP 加密方式",
                "type": "select",
                "default_value": "ssl",
                "category": "connection",
                "options": [
                    {"value": "ssl", "label": "SSL/TLS（通常为 993）"},
                    {"value": "starttls", "label": "STARTTLS（通常为 143）"},
                    {"value": "plain", "label": "明文（仅限受信任内网）"},
                ],
            },
            {
                "key": "domain_imap_folder",
                "label": "收件箱文件夹",
                "placeholder": "INBOX",
                "default_value": "INBOX",
                "category": "connection",
            },
            {
                "key": "domain_imap_local_prefix",
                "label": "新地址前缀",
                "placeholder": "reg",
                "default_value": "reg",
                "category": "connection",
                "hint": "生成示例：reg-a1b2c3d4e5f6@abaifly.edu.kg。",
            },
            {
                "key": "domain_imap_poll_interval",
                "label": "轮询间隔秒",
                "placeholder": "3",
                "default_value": "3",
                "category": "connection",
            },
            {
                "key": "domain_imap_search_limit",
                "label": "每次检查最近邮件数",
                "placeholder": "40",
                "default_value": "40",
                "category": "connection",
            },
            {
                "key": "domain_imap_state_file",
                "label": "地址占用状态文件",
                "placeholder": "默认 data/.domain_imap_mailbox_state.json",
                "category": "connection",
                "hint": "用于避免程序重启后重复使用已生成的域名邮箱地址。",
            },
            {
                "key": "domain_imap_username",
                "label": "IMAP 登录账号",
                "placeholder": "catchall@abaifly.edu.kg",
                "category": "auth",
            },
            {
                "key": "domain_imap_password",
                "label": "IMAP 密码",
                "type": "text",
                "secret": True,
                "category": "auth",
            },
        ],
    },
    {
        "provider_type": "mailbox",
        "provider_key": "domain_inbucket",
        "label": "自有域名邮箱（Inbucket）",
        "description": "通过 Inbucket 的 SMTP 收信和本地 API，为每次注册生成独立域名邮箱并读取验证码。",
        "driver_type": "domain_inbucket",
        "default_auth_mode": "api",
        "enabled": True,
        "category": "custom",
        "auth_modes": [{"value": "api", "label": "Inbucket API"}],
        "fields": [
            {
                "key": "inbucket_domain",
                "label": "邮箱域名",
                "placeholder": "abaifly.edu.kg",
                "category": "connection",
                "hint": "域名 MX 必须投递到 Inbucket 的 SMTP 服务。",
            },
            {
                "key": "inbucket_api_url",
                "label": "Inbucket API 地址",
                "placeholder": "http://127.0.0.1:9000/api/v1",
                "default_value": "http://127.0.0.1:9000/api/v1",
                "category": "connection",
                "hint": "Inbucket Web/API 默认端口为 9000；生产部署请保持 API 仅本机可访问。",
            },
            {
                "key": "inbucket_local_prefix",
                "label": "新地址前缀",
                "placeholder": "reg",
                "default_value": "reg",
                "category": "connection",
            },
            {
                "key": "inbucket_poll_interval",
                "label": "轮询间隔秒",
                "placeholder": "3",
                "default_value": "3",
                "category": "connection",
            },
            {
                "key": "inbucket_request_timeout",
                "label": "API 请求超时秒",
                "placeholder": "15",
                "default_value": "15",
                "category": "connection",
            },
            {
                "key": "inbucket_state_file",
                "label": "地址占用状态文件",
                "placeholder": "默认 data/.inbucket_domain_mailbox_state.json",
                "category": "connection",
            },
        ],
    },
    {
        "provider_type": "captcha",
        "provider_key": "yescaptcha_api",
        "label": "YesCaptcha",
        "description": "YesCaptcha 云端验证码识别服务，支持 Turnstile 等类型",
        "driver_type": "yescaptcha_api",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {"key": "yescaptcha_key", "label": "Client Key", "secret": True},
        ],
    },
    {
        "provider_type": "captcha",
        "provider_key": "twocaptcha_api",
        "label": "2Captcha",
        "description": "2Captcha 云端验证码识别服务，支持 Turnstile 等类型",
        "driver_type": "twocaptcha_api",
        "default_auth_mode": "apikey",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [{"value": "apikey", "label": "API Key"}],
        "fields": [
            {"key": "twocaptcha_key", "label": "API Key", "secret": True},
        ],
    },
    {
        "provider_type": "captcha",
        "provider_key": "local_solver",
        "label": "本地验证码求解器",
        "description": "调用本地 api_solver 服务（Camoufox/patchright）解 Turnstile 验证码",
        "driver_type": "local_solver",
        "default_auth_mode": "",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [],
        "fields": [
            {"key": "solver_url", "label": "Solver 地址", "placeholder": "http://localhost:8889"},
        ],
    },
    {
        "provider_type": "captcha",
        "provider_key": "manual",
        "label": "人工打码",
        "description": "阻塞等待用户手动输入验证码，适用于调试场景",
        "driver_type": "manual",
        "default_auth_mode": "",
        "enabled": True,
        "category": "thirdparty",
        "auth_modes": [],
        "fields": [],
    },
    # ── proxy ────────────────────────────────────────────────────────
    {
        "provider_type": "proxy",
        "provider_key": "api_extract",
        "label": "API 提取代理",
        "description": "通过 HTTP API 动态提取代理 IP 列表，适用于大多数代理商的 API 提取接口",
        "driver_type": "api_extract",
        "default_auth_mode": "",
        "enabled": False,
        "category": "thirdparty",
        "auth_modes": [],
        "fields": [
            {"key": "proxy_api_url", "label": "API 地址", "placeholder": "https://provider.com/api/get_proxy?key=xxx"},
            {"key": "proxy_protocol", "label": "协议", "placeholder": "http / socks5"},
            {"key": "proxy_username", "label": "用户名 (可选)"},
            {"key": "proxy_password", "label": "密码 (可选)", "secret": True},
        ],
    },
    {
        "provider_type": "proxy",
        "provider_key": "rotating_gateway",
        "label": "旋转网关代理",
        "description": "固定入口地址，每次请求自动分配不同出口 IP，适用于 BrightData / Oxylabs / IPRoyal 等",
        "driver_type": "rotating_gateway",
        "default_auth_mode": "",
        "enabled": False,
        "category": "thirdparty",
        "auth_modes": [],
        "fields": [
            {"key": "proxy_gateway_url", "label": "网关地址", "placeholder": "http://user:pass@gate.example.com:7777"},
        ],
    },
]


class ProviderDefinitionsRepository:

    def ensure_seeded(self) -> None:
        """将内置 provider definition 种子数据写入数据库。

        新增的插入，已存在的更新字段定义（label、description、fields 等），
        确保代码升级后内置 provider 的元数据能同步到数据库。
        """
        with Session(engine) as session:
            existing: dict[str, ProviderDefinitionModel] = {}
            for row in session.exec(select(ProviderDefinitionModel)).all():
                key = f"{row.provider_type}::{row.provider_key}"
                existing[key] = row

            changed = False
            for seed in _BUILTIN_DEFINITIONS:
                key = f"{seed['provider_type']}::{seed['provider_key']}"
                item = existing.get(key)

                if item is None:
                    # 新增
                    item = ProviderDefinitionModel(
                        provider_type=seed["provider_type"],
                        provider_key=seed["provider_key"],
                        created_at=_utcnow(),
                    )
                    logger.info("种子数据: 新增 %s/%s", seed["provider_type"], seed["provider_key"])

                # 更新元数据（每次启动都同步，确保代码变更生效）
                item.label = seed.get("label", seed["provider_key"])
                item.description = seed.get("description", "")
                item.driver_type = seed.get("driver_type", seed["provider_key"])
                item.default_auth_mode = seed.get("default_auth_mode", "")
                item.enabled = (
                    seed["provider_key"] in SUPPORTED_MAILBOX_PROVIDER_KEYS
                    if seed["provider_type"] == "mailbox"
                    else seed.get("enabled", True)
                )
                item.is_builtin = True
                item.category = seed.get("category", "")
                item.set_auth_modes(list(seed.get("auth_modes") or []))
                item.set_fields(list(seed.get("fields") or []))
                if not item.get_metadata():
                    # 只在 metadata 为空时写入种子值，避免覆盖用户自定义的 pipeline
                    item.set_metadata(dict(seed.get("metadata") or {}))
                item.updated_at = _utcnow()
                session.add(item)
                changed = True

            # Keep historical/custom mailbox definitions in the database so
            # upgrades are non-destructive, but remove them from active use.
            for item in existing.values():
                if (
                    item.provider_type == "mailbox"
                    and item.provider_key not in SUPPORTED_MAILBOX_PROVIDER_KEYS
                    and item.enabled
                ):
                    item.enabled = False
                    item.updated_at = _utcnow()
                    session.add(item)
                    changed = True

            if changed:
                session.commit()

    # ── 查询（全部从 DB） ────────────────────────────────────────────

    def list_by_type(self, provider_type: str, *, enabled_only: bool = False) -> list[ProviderDefinitionModel]:
        with Session(engine) as session:
            query = select(ProviderDefinitionModel).where(ProviderDefinitionModel.provider_type == provider_type)
            if enabled_only:
                query = query.where(ProviderDefinitionModel.enabled == True)  # noqa: E712
            items = session.exec(query.order_by(ProviderDefinitionModel.id)).all()
            if provider_type == "mailbox":
                items = [item for item in items if item.provider_key in SUPPORTED_MAILBOX_PROVIDER_KEYS]
            return items

    def get_by_key(self, provider_type: str, provider_key: str) -> ProviderDefinitionModel | None:
        with Session(engine) as session:
            return session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == provider_type)
                .where(ProviderDefinitionModel.provider_key == provider_key)
            ).first()

    def list_driver_templates(self, provider_type: str) -> list[dict]:
        """从 DB 读取：按 driver_type 去重，返回可用驱动模板列表。"""
        with Session(engine) as session:
            definitions = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == provider_type)
                .order_by(ProviderDefinitionModel.is_builtin.desc(), ProviderDefinitionModel.id)
            ).all()
        seen: dict[str, dict] = {}
        for d in definitions:
            if provider_type == "mailbox" and d.provider_key not in SUPPORTED_MAILBOX_PROVIDER_KEYS:
                continue
            dt = d.driver_type or ""
            if dt and dt not in seen:
                seen[dt] = {
                    "provider_type": d.provider_type,
                    "provider_key": d.provider_key,
                    "driver_type": dt,
                    "label": d.label,
                    "description": d.description,
                    "default_auth_mode": d.default_auth_mode,
                    "auth_modes": d.get_auth_modes(),
                    "fields": d.get_fields(),
                }
        return list(seen.values())

    def _get_driver_defaults(self, provider_type: str, driver_type: str) -> dict | None:
        """从 DB 中查找同 driver_type 的已有 definition 作为模板。"""
        with Session(engine) as session:
            ref = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == provider_type)
                .where(ProviderDefinitionModel.driver_type == driver_type)
                .order_by(ProviderDefinitionModel.is_builtin.desc(), ProviderDefinitionModel.id)
            ).first()
            if not ref:
                return None
            return {
                "default_auth_mode": ref.default_auth_mode,
                "auth_modes": ref.get_auth_modes(),
                "fields": ref.get_fields(),
            }

    # ── 写入 ────────────────────────────────────────────────────────

    def save(
        self,
        *,
        definition_id: int | None,
        provider_type: str,
        provider_key: str,
        label: str,
        description: str,
        driver_type: str,
        enabled: bool,
        default_auth_mode: str = "",
        metadata: dict | None = None,
    ) -> ProviderDefinitionModel:
        defaults = self._get_driver_defaults(provider_type, driver_type)

        with Session(engine) as session:
            if definition_id:
                item = session.get(ProviderDefinitionModel, definition_id)
                if not item:
                    raise ValueError("provider definition 不存在")
            else:
                item = session.exec(
                    select(ProviderDefinitionModel)
                    .where(ProviderDefinitionModel.provider_type == provider_type)
                    .where(ProviderDefinitionModel.provider_key == provider_key)
                ).first()
                if not item:
                    item = ProviderDefinitionModel(
                        provider_type=provider_type,
                        provider_key=provider_key,
                    )
                    item.created_at = _utcnow()

            item.provider_type = provider_type
            item.provider_key = provider_key
            item.label = label or provider_key
            item.description = description or ""
            item.driver_type = driver_type
            item.default_auth_mode = default_auth_mode or item.default_auth_mode or (defaults.get("default_auth_mode", "") if defaults else "")
            item.enabled = bool(enabled)
            if not item.get_auth_modes() and defaults:
                item.set_auth_modes(list(defaults.get("auth_modes") or []))
            if not item.get_fields() and defaults:
                item.set_fields(list(defaults.get("fields") or []))
            item.set_metadata(dict(metadata or {}))
            item.updated_at = _utcnow()
            session.add(item)
            session.commit()
            session.refresh(item)
            return item

    def delete(self, definition_id: int) -> bool:
        with Session(engine) as session:
            item = session.get(ProviderDefinitionModel, definition_id)
            if not item:
                return False
            has_settings = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == item.provider_type)
                .where(ProviderSettingModel.provider_key == item.provider_key)
            ).first()
            if has_settings:
                raise ValueError("请先删除对应 provider 配置，再删除 definition")
            session.delete(item)
            session.commit()
            return True
