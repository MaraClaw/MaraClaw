"""Finite initialization adapter for the continuously reconciled WeChat manager."""

from app.runtime.tasks import OwnedTasks
from app.services.wechat_channel import WeChatPollManager


class WeChatRuntime:
    def __init__(self, manager: WeChatPollManager, tasks: OwnedTasks) -> None:
        self.manager: WeChatPollManager = manager
        self.tasks: OwnedTasks = tasks

    async def start_all(self) -> None:
        await self.manager.reconcile_clients()
        self.tasks.start("wechat-reconcile", self.manager.start_all(), requirement="loop")

    async def stop_all(self) -> None:
        await self.manager.stop_all()
