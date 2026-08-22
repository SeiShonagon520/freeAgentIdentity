"""定时任务调度 - 账号有效性检测、trial 到期提醒。"""
from datetime import datetime, timedelta, timezone, tzinfo
import os
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import Session, select

from .account_graph import load_account_graphs, patch_account_graph
from .base_platform import AccountStatus, RegisterConfig
from .db import AccountModel, TaskModel, engine
from .platform_accounts import build_platform_account
from .registry import get, load_all


DAILY_401_SCHEDULE_SOURCE = "daily_401_check"
DEFAULT_DAILY_401_HOUR = 3
DEFAULT_DAILY_401_CONCURRENCY = 100
DEFAULT_DAILY_401_TIMEZONE = "Asia/Shanghai"
SCHEDULER_POLL_SECONDS = 60
TRIAL_EXPIRY_CHECK_SECONDS = 3600


def _daily_401_enabled() -> bool:
    """Whether the scheduler may auto-create the daily 401 maintenance task.

    Defaults to enabled for backward compatibility; set
    ``DAILY_401_CHECK_ENABLED=0`` to run 401 verification manually (e.g. from
    the account list page) instead of every day at 03:00.
    """
    return os.getenv("DAILY_401_CHECK_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _configured_timezone() -> tzinfo:
    name = os.getenv("DAILY_401_CHECK_TIMEZONE", DEFAULT_DAILY_401_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # Some Windows/PyInstaller deployments do not bundle the IANA tzdata.
        # Beijing time has no daylight-saving transition, so UTC+8 is a safe
        # fallback for the default schedule.
        if name == DEFAULT_DAILY_401_TIMEZONE:
            return timezone(timedelta(hours=8), name=DEFAULT_DAILY_401_TIMEZONE)
        print(f"[Scheduler] 未找到时区 {name}，定时 401 验活将使用 UTC")
        return timezone.utc


class Scheduler:
    def __init__(
        self,
        *,
        daily_401_hour: int | None = None,
        daily_401_concurrency: int | None = None,
        schedule_timezone: tzinfo | None = None,
    ):
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_daily_401_date = ""
        self.daily_401_hour = (
            min(max(int(daily_401_hour), 0), 23)
            if daily_401_hour is not None
            else _env_int(
                "DAILY_401_CHECK_HOUR",
                DEFAULT_DAILY_401_HOUR,
                minimum=0,
                maximum=23,
            )
        )
        self.daily_401_concurrency = (
            min(max(int(daily_401_concurrency), 1), 200)
            if daily_401_concurrency is not None
            else _env_int(
                "DAILY_401_CHECK_CONCURRENCY",
                DEFAULT_DAILY_401_CONCURRENCY,
                minimum=1,
                maximum=200,
            )
        )
        self.schedule_timezone = schedule_timezone or _configured_timezone()

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="account-scheduler",
        )
        self._thread.start()
        print(
            f"[Scheduler] 已启动，每天 {self.daily_401_hour:02d}:00 创建 401 验活任务，"
            f"并发 {self.daily_401_concurrency}"
            + ("" if _daily_401_enabled() else "（自动 401 验活已关闭，改为手动触发）")
        )

    def stop(self):
        self._running = False
        self._stop_event.set()

    def _loop(self):
        next_trial_check = 0.0
        while self._running:
            now_monotonic = time.monotonic()
            if now_monotonic >= next_trial_check:
                try:
                    self.check_trial_expiry()
                except Exception as e:
                    print(f"[Scheduler] trial 到期检查错误: {e}")
                next_trial_check = now_monotonic + TRIAL_EXPIRY_CHECK_SECONDS
            try:
                self.check_daily_401_task()
            except Exception as e:
                # Keep retrying every minute. A temporary SQLite/network
                # failure must not permanently skip the day's maintenance.
                print(f"[Scheduler] 定时 401 验活错误: {e}")
            self._stop_event.wait(SCHEDULER_POLL_SECONDS)

    def _daily_401_task_exists(self, schedule_date: str) -> bool:
        with Session(engine) as session:
            tasks = session.exec(
                select(TaskModel).where(
                    TaskModel.type == "refresh_token_check",
                    TaskModel.payload_json.contains(DAILY_401_SCHEDULE_SOURCE),
                )
            ).all()
        for task in tasks:
            payload = task.get_payload()
            if (
                payload.get("schedule_source") == DAILY_401_SCHEDULE_SOURCE
                and payload.get("schedule_date") == schedule_date
            ):
                return True
        return False

    def check_daily_401_task(self, now: datetime | None = None) -> dict | None:
        """Create today's 401 maintenance task once the local clock reaches 03:00."""
        if not _daily_401_enabled():
            return None
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            local_now = current.replace(tzinfo=self.schedule_timezone)
        else:
            local_now = current.astimezone(self.schedule_timezone)
        if local_now.hour < self.daily_401_hour:
            return None

        schedule_date = local_now.date().isoformat()
        if self._last_daily_401_date == schedule_date:
            return None
        if self._daily_401_task_exists(schedule_date):
            self._last_daily_401_date = schedule_date
            return None

        from application.tasks import create_refresh_token_check_task
        from services.task_runtime import task_runtime

        task = create_refresh_token_check_task(
            platform="chatgpt",
            concurrency=self.daily_401_concurrency,
            schedule_date=schedule_date,
            browser=True,
        )
        self._last_daily_401_date = schedule_date
        task_runtime.wake_up()
        print(
            f"[Scheduler] 已创建 {schedule_date} 的 401 验活任务: "
            f"{task['task_id']}（并发 {self.daily_401_concurrency}）"
        )
        return task

    def check_trial_expiry(self):
        """检查 trial 到期账号，更新状态"""
        now = int(datetime.now(timezone.utc).timestamp())
        with Session(engine) as s:
            accounts = s.exec(select(AccountModel)).all()
            graphs = load_account_graphs(s, [int(acc.id or 0) for acc in accounts if acc.id])
            updated = 0
            for acc in accounts:
                graph = graphs.get(int(acc.id or 0), {})
                if graph.get("lifecycle_status") != "trial":
                    continue
                trial_end_time = int((graph.get("overview") or {}).get("trial_end_time") or 0)
                if trial_end_time and trial_end_time < now:
                    acc.updated_at = datetime.now(timezone.utc)
                    patch_account_graph(s, acc, lifecycle_status=AccountStatus.EXPIRED.value)
                    s.add(acc)
                    updated += 1
            s.commit()
            if updated:
                print(f"[Scheduler] {updated} 个 trial 账号已到期")

    def check_accounts_valid(self, platform: str = None, limit: int = 50):
        """批量检测账号有效性"""
        load_all()
        with Session(engine) as s:
            q = select(AccountModel)
            if platform:
                q = q.where(AccountModel.platform == platform)
            q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
            accounts = s.exec(q.limit(limit)).all()
            graphs = load_account_graphs(s, [int(acc.id or 0) for acc in accounts if acc.id])
            accounts = [
                acc for acc in accounts
                if graphs.get(int(acc.id or 0), {}).get("lifecycle_status") in {"registered", "trial", "subscribed"}
            ]

        results = {"valid": 0, "invalid": 0, "error": 0}
        for acc in accounts:
            try:
                PlatformCls = get(acc.platform)
                plugin = PlatformCls(config=RegisterConfig())
                with Session(engine) as s:
                    current = s.get(AccountModel, acc.id)
                    if not current:
                        continue
                    account_obj = build_platform_account(s, current)
                valid = plugin.check_valid(account_obj)
                with Session(engine) as s:
                    a = s.get(AccountModel, acc.id)
                    if a:
                        a.updated_at = datetime.now(timezone.utc)
                        summary_updates = {"checked_at": _utcnow_iso(), "valid": valid}
                        if hasattr(plugin, "get_last_check_overview"):
                            summary_updates.update(plugin.get_last_check_overview() or {})
                        patch_account_graph(
                            s,
                            a,
                            summary_updates=summary_updates,
                        )
                        s.add(a)
                        s.commit()
                if valid:
                    results["valid"] += 1
                else:
                    results["invalid"] += 1
            except Exception:
                results["error"] += 1
        return results


scheduler = Scheduler()
