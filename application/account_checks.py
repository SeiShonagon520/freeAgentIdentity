from __future__ import annotations

from application.tasks import create_refresh_token_check_task
from services.task_runtime import task_runtime


class AccountChecksService:
    def check_refresh_tokens_async(
        self,
        platform: str = "chatgpt",
        concurrency: int | None = None,
        proxy_node: str | None = None,
        browser: bool = True,
    ) -> dict:
        task = create_refresh_token_check_task(
            platform or "chatgpt",
            concurrency,
            proxy_node=proxy_node,
            browser=bool(browser),
        )
        task_runtime.wake_up()
        return task
