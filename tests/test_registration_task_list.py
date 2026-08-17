from __future__ import annotations

from application.tasks import create_register_task, list_tasks


def test_unlimited_registration_task_has_infinite_progress_label():
    task = create_register_task({"count": 0, "concurrency": 50})

    item = next(row for row in list_tasks(task_type="register") if row["task_id"] == task["task_id"])
    assert item["progress"] == "0/∞"
    assert item["progress_detail"]["unlimited"] is True
