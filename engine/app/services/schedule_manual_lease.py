from datetime import datetime
from typing import override

from app.dao.schedule_manual_run_dao import schedule_manual_run_dao
from app.services.schedule_lease import ScheduleLeaseHold


class ManualScheduleLeaseHold(ScheduleLeaseHold):
    @override
    async def renew_ownership(self, now: datetime) -> bool:
        return await schedule_manual_run_dao.renew(self.schedule_id, self.token, now)
