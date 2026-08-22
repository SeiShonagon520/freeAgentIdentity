from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from core.db import TaskModel, engine
from core.scheduler import Scheduler


BEIJING_TIME = timezone(timedelta(hours=8))


def test_daily_401_task_is_created_once_after_3am_and_survives_restart(monkeypatch):
    from services.task_runtime import task_runtime

    wakeups: list[bool] = []
    monkeypatch.setattr(task_runtime, "wake_up", lambda: wakeups.append(True))
    scheduler = Scheduler(
        daily_401_hour=3,
        daily_401_concurrency=50,
        schedule_timezone=BEIJING_TIME,
    )

    assert scheduler.check_daily_401_task(
        datetime(2026, 7, 30, 2, 59, tzinfo=BEIJING_TIME)
    ) is None

    created = scheduler.check_daily_401_task(
        datetime(2026, 7, 30, 3, 0, tzinfo=BEIJING_TIME)
    )
    assert created is not None
    assert wakeups == [True]

    with Session(engine) as session:
        tasks = session.exec(select(TaskModel)).all()
        assert len(tasks) == 1
        payload = tasks[0].get_payload()
        assert payload["platform"] == "chatgpt"
        assert payload["concurrency"] == 50
        assert payload["browser"] is True
        assert payload["schedule_source"] == "daily_401_check"
        assert payload["schedule_date"] == "2026-07-30"

    assert scheduler.check_daily_401_task(
        datetime(2026, 7, 30, 23, 0, tzinfo=BEIJING_TIME)
    ) is None

    restarted_scheduler = Scheduler(
        daily_401_hour=3,
        daily_401_concurrency=50,
        schedule_timezone=BEIJING_TIME,
    )
    assert restarted_scheduler.check_daily_401_task(
        datetime(2026, 7, 30, 5, 0, tzinfo=BEIJING_TIME)
    ) is None
    assert wakeups == [True]

    with Session(engine) as session:
        assert len(session.exec(select(TaskModel)).all()) == 1
