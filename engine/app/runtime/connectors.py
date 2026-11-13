"""Finite initialization adapter for the continuously reconciled WeChat manager."""

from app.runtime.tasks import OwnedTasks
from app.services.wechat_channel import PROGRESS_BUDGET_SECONDS as WECHAT_BUDGET_SECONDS, WeChatPollManager


class WeChatRuntime:
    def __init__(self, manager: WeChatPollManager, tasks: OwnedTasks) -> None:
        self.manager: WeChatPollManager = manager
        self.tasks: OwnedTasks = tasks

    async def start_all(self) -> None:
        await self.manager.reconcile_clients()
        self.tasks.start(
            "wechat-reconcile",
            self.manager.start_all(),
            requirement="loop",
            progress_budget_seconds=WECHAT_BUDGET_SECONDS,
        )

    async def stop_all(self) -> None:
        await self.manager.stop_all()
