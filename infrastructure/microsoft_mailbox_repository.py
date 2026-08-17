from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import func, or_, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from core.db import MicrosoftMailboxModel, ProviderSettingModel, engine
from core.secret_store import decrypt_secret, encrypt_secret


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _email_key(value: object) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class MicrosoftMailboxRecord:
    id: int
    email: str
    password: str
    login_account: str
    imap_host: str
    imap_port: str
    imap_account_type: str
    imap_security: str
    smtp_host: str
    smtp_port: str
    smtp_security: str
    note: str
    proxy_mode: str
    proxy: str
    label: str
    recovery_email: str
    recovery_password: str
    client_id: str
    refresh_token: str
    totp_secret: str
    source_format: str
    use_count: int
    max_uses: int
    status: str


class MicrosoftMailboxRepository:
    def _record(self, row) -> MicrosoftMailboxRecord:
        getter = row.get if hasattr(row, "get") else lambda key, default=None: getattr(row, key, default)
        return MicrosoftMailboxRecord(
            id=int(getter("id") or 0),
            email=str(getter("email") or ""),
            password=decrypt_secret(getter("password_ciphertext") or ""),
            login_account=str(getter("login_account") or ""),
            imap_host=str(getter("imap_host") or ""),
            imap_port=str(getter("imap_port") or ""),
            imap_account_type=str(getter("imap_account_type") or ""),
            imap_security=str(getter("imap_security") or ""),
            smtp_host=str(getter("smtp_host") or ""),
            smtp_port=str(getter("smtp_port") or ""),
            smtp_security=str(getter("smtp_security") or ""),
            note=str(getter("note") or ""),
            proxy_mode=str(getter("proxy_mode") or ""),
            proxy=str(getter("proxy") or ""),
            label=str(getter("label") or ""),
            recovery_email=str(getter("recovery_email") or ""),
            recovery_password=decrypt_secret(getter("recovery_password_ciphertext") or ""),
            client_id=str(getter("client_id") or ""),
            refresh_token=decrypt_secret(getter("refresh_token_ciphertext") or ""),
            totp_secret=decrypt_secret(getter("totp_secret_ciphertext") or ""),
            source_format=str(getter("source_format") or ""),
            use_count=int(getter("use_count") or 0),
            max_uses=max(int(getter("max_uses") or 6), 1),
            status=str(getter("status") or "available"),
        )

    @staticmethod
    def _payload(entry, *, max_uses: int) -> dict:
        now = _utcnow()
        email = str(getattr(entry, "email", "") or "").strip()
        return {
            "email": email,
            "email_key": _email_key(email),
            "password_ciphertext": encrypt_secret(getattr(entry, "password", "")),
            "login_account": str(getattr(entry, "login_account", "") or email),
            "imap_host": str(getattr(entry, "imap_host", "") or ""),
            "imap_port": str(getattr(entry, "imap_port", "") or ""),
            "imap_account_type": str(getattr(entry, "imap_account_type", "") or ""),
            "imap_security": str(getattr(entry, "imap_security", "") or ""),
            "smtp_host": str(getattr(entry, "smtp_host", "") or ""),
            "smtp_port": str(getattr(entry, "smtp_port", "") or ""),
            "smtp_security": str(getattr(entry, "smtp_security", "") or ""),
            "note": str(getattr(entry, "note", "") or ""),
            "proxy_mode": str(getattr(entry, "proxy_mode", "") or ""),
            "proxy": str(getattr(entry, "proxy", "") or ""),
            "label": str(getattr(entry, "label", "") or ""),
            "recovery_email": str(getattr(entry, "recovery_email", "") or ""),
            "recovery_password_ciphertext": encrypt_secret(
                getattr(entry, "recovery_password", "")
            ),
            "client_id": str(getattr(entry, "client_id", "") or ""),
            "refresh_token_ciphertext": encrypt_secret(
                getattr(entry, "refresh_token", "")
            ),
            "totp_secret_ciphertext": encrypt_secret(getattr(entry, "totp_secret", "")),
            "source_format": str(getattr(entry, "source_format", "") or ""),
            "max_uses": max(int(max_uses or 6), 1),
            "created_at": now,
            "updated_at": now,
        }

    def import_entries(self, entries: Iterable, *, max_uses: int = 6) -> dict:
        unique: dict[str, object] = {}
        for entry in entries:
            key = _email_key(getattr(entry, "email", ""))
            if key:
                unique[key] = entry
        if not unique:
            return {"received": 0, "inserted": 0, "updated": 0, **self.stats()}

        keys = list(unique)
        existing: set[str] = set()
        with Session(engine) as session:
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                existing.update(
                    str(value)
                    for value in session.exec(
                        select(MicrosoftMailboxModel.email_key).where(
                            MicrosoftMailboxModel.email_key.in_(chunk)
                        )
                    ).all()
                )

        payloads = [self._payload(entry, max_uses=max_uses) for entry in unique.values()]
        table = MicrosoftMailboxModel.__table__
        statement = sqlite_insert(table).on_conflict_do_update(
            index_elements=[table.c.email_key],
            set_={
                column: getattr(sqlite_insert(table).excluded, column)
                for column in (
                    "email",
                    "password_ciphertext",
                    "login_account",
                    "imap_host",
                    "imap_port",
                    "imap_account_type",
                    "imap_security",
                    "smtp_host",
                    "smtp_port",
                    "smtp_security",
                    "note",
                    "proxy_mode",
                    "proxy",
                    "label",
                    "recovery_email",
                    "recovery_password_ciphertext",
                    "client_id",
                    "refresh_token_ciphertext",
                    "totp_secret_ciphertext",
                    "source_format",
                    "max_uses",
                    "updated_at",
                )
            },
        )
        with engine.begin() as connection:
            connection.execute(statement, payloads)

        stats = self.stats()
        return {
            "received": len(unique),
            "inserted": len(set(keys) - existing),
            "updated": len(existing),
            **stats,
        }

    def reserve(self, *, allow_reuse: bool = False) -> MicrosoftMailboxRecord:
        if allow_reuse:
            with Session(engine) as session:
                row = session.exec(
                    select(MicrosoftMailboxModel)
                    .where(MicrosoftMailboxModel.status != "disabled")
                    .order_by(MicrosoftMailboxModel.id)
                    .limit(1)
                ).first()
            if row is None:
                raise RuntimeError("本地微软邮箱池为空")
            return self._record(row)

        now = _utcnow().isoformat()
        statement = text(
            """
            UPDATE microsoft_mailboxes
            SET
                use_count = use_count + 1,
                status = CASE
                    WHEN use_count + 1 >= max_uses THEN 'exhausted'
                    ELSE 'available'
                END,
                last_reserved_at = :now,
                updated_at = :now
            WHERE id = (
                SELECT id
                FROM microsoft_mailboxes
                WHERE status = 'available' AND use_count < max_uses
                ORDER BY use_count, id
                LIMIT 1
            )
            RETURNING *
            """
        )
        with engine.begin() as connection:
            row = connection.execute(statement, {"now": now}).mappings().first()
        if row is None:
            stats = self.stats()
            raise RuntimeError(
                "本地微软邮箱池已用尽: "
                f"total={stats['total']}, capacity={stats['capacity']}"
            )
        return self._record(row)

    def peek(self) -> MicrosoftMailboxRecord:
        with Session(engine) as session:
            row = session.exec(
                select(MicrosoftMailboxModel)
                .where(MicrosoftMailboxModel.status == "available")
                .where(MicrosoftMailboxModel.use_count < MicrosoftMailboxModel.max_uses)
                .order_by(MicrosoftMailboxModel.use_count, MicrosoftMailboxModel.id)
                .limit(1)
            ).first()
        if row is None:
            stats = self.stats()
            raise RuntimeError(
                "本地微软邮箱池已用尽: "
                f"total={stats['total']}, capacity={stats['capacity']}"
            )
        return self._record(row)

    def get_by_parent_email(self, email: str) -> MicrosoftMailboxRecord | None:
        with Session(engine) as session:
            row = session.exec(
                select(MicrosoftMailboxModel).where(
                    MicrosoftMailboxModel.email_key == _email_key(email)
                )
            ).first()
        return self._record(row) if row is not None else None

    def disable(self, email: str) -> bool:
        """Permanently remove a mailbox with unusable credentials from allocation."""
        key = _email_key(email)
        if not key:
            return False
        now = _utcnow().isoformat()
        statement = text(
            """
            UPDATE microsoft_mailboxes
            SET status = 'disabled', updated_at = :updated_at
            WHERE email_key = :email_key AND status != 'disabled'
            """
        )
        with engine.begin() as connection:
            result = connection.execute(
                statement,
                {"email_key": key, "updated_at": now},
            )
        return bool(result.rowcount)

    def stats(self) -> dict:
        with Session(engine) as session:
            total, capacity, used, available = session.exec(
                select(
                    func.count(MicrosoftMailboxModel.id),
                    func.coalesce(func.sum(MicrosoftMailboxModel.max_uses), 0),
                    func.coalesce(func.sum(MicrosoftMailboxModel.use_count), 0),
                    func.coalesce(
                        func.sum(
                            MicrosoftMailboxModel.max_uses - MicrosoftMailboxModel.use_count
                        ),
                        0,
                    ),
                ).where(MicrosoftMailboxModel.status != "disabled")
            ).one()
            exhausted = session.exec(
                select(func.count(MicrosoftMailboxModel.id)).where(
                    MicrosoftMailboxModel.status == "exhausted"
                )
            ).one()
        return {
            "total": int(total or 0),
            "capacity": int(capacity or 0),
            "used": int(used or 0),
            "remaining": int(available or 0),
            "exhausted": int(exhausted or 0),
        }

    def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        search: str = "",
    ) -> dict:
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 50), 1), 200)
        with Session(engine) as session:
            query = select(MicrosoftMailboxModel)
            count_query = select(func.count(MicrosoftMailboxModel.id))
            if status:
                query = query.where(MicrosoftMailboxModel.status == status)
                count_query = count_query.where(MicrosoftMailboxModel.status == status)
            if search.strip():
                pattern = f"%{search.strip().lower()}%"
                predicate = or_(
                    MicrosoftMailboxModel.email_key.like(pattern),
                    MicrosoftMailboxModel.label.like(pattern),
                )
                query = query.where(predicate)
                count_query = count_query.where(predicate)
            total = int(session.exec(count_query).one() or 0)
            rows = session.exec(
                query.order_by(MicrosoftMailboxModel.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        return {
            "items": [
                {
                    "id": int(row.id or 0),
                    "email": row.email,
                    "use_count": int(row.use_count or 0),
                    "max_uses": int(row.max_uses or 6),
                    "status": row.status,
                    "source_format": row.source_format,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else "",
                }
                for row in rows
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def apply_minimum_usage(self, usage_by_email: dict[str, int]) -> None:
        payloads = [
            {"email_key": _email_key(email), "use_count": max(int(count or 0), 0)}
            for email, count in usage_by_email.items()
            if _email_key(email)
        ]
        if not payloads:
            return
        statement = text(
            """
            UPDATE microsoft_mailboxes
            SET
                use_count = MIN(max_uses, MAX(use_count, :use_count)),
                status = CASE
                    WHEN MIN(max_uses, MAX(use_count, :use_count)) >= max_uses
                        THEN 'exhausted'
                    ELSE 'available'
                END,
                updated_at = :updated_at
            WHERE email_key = :email_key
            """
        )
        now = _utcnow().isoformat()
        for payload in payloads:
            payload["updated_at"] = now
        with engine.begin() as connection:
            connection.execute(statement, payloads)


def migrate_legacy_microsoft_mailbox_pool() -> dict:
    with Session(engine) as session:
        setting = session.exec(
            select(ProviderSettingModel)
            .where(ProviderSettingModel.provider_type == "mailbox")
            .where(ProviderSettingModel.provider_key == "local_ms_pool")
        ).first()
        if setting is None:
            return {"migrated": 0}
        config = setting.get_config()
        auth = setting.get_auth()

    pool_text = str(auth.get("local_ms_pool_text") or config.get("local_ms_pool_text") or "")
    pool_file = str(config.get("local_ms_pool_file") or auth.get("local_ms_pool_file") or "").strip()
    chunks = [pool_text] if pool_text.strip() else []
    if pool_file:
        path = Path(pool_file).expanduser()
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8-sig"))
    combined = "\n".join(chunks)
    if not combined.strip():
        return {"migrated": 0}

    from core.local_ms_mailbox import parse_local_ms_pool_rows

    entries = parse_local_ms_pool_rows(combined)
    if not entries:
        return {"migrated": 0}

    repository = MicrosoftMailboxRepository()
    result = repository.import_entries(entries, max_uses=6)

    state_path = Path(
        str(config.get("local_ms_pool_state_file") or "").strip()
        or Path(__file__).resolve().parent.parent / "data" / ".local_ms_mailbox_pool_state.json"
    )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {"used": {}}
    usage: Counter[str] = Counter()
    for key in (state.get("used") or {}).keys():
        normalized = str(key or "").strip().lower()
        parent = normalized.split("#sub-", 1)[0]
        if parent:
            usage[parent] += 1
    repository.apply_minimum_usage(dict(usage))

    with Session(engine) as session:
        setting = session.exec(
            select(ProviderSettingModel)
            .where(ProviderSettingModel.provider_type == "mailbox")
            .where(ProviderSettingModel.provider_key == "local_ms_pool")
        ).first()
        if setting is not None:
            config = setting.get_config()
            auth = setting.get_auth()
            for key in (
                "local_ms_pool_text",
                "local_ms_pool_file",
                "local_ms_pool_state_file",
                "local_ms_pool_allow_reuse",
            ):
                config.pop(key, None)
                auth.pop(key, None)
            metadata = setting.get_metadata()
            metadata["database_pool_migrated_at"] = _utcnow().isoformat()
            setting.set_config(config)
            setting.set_auth(auth)
            setting.set_metadata(metadata)
            setting.updated_at = _utcnow()
            session.add(setting)
            session.commit()

    return {"migrated": len(entries), **result, **repository.stats()}
