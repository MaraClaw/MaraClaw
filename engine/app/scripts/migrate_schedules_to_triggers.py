"""Migrate existing AgentSchedule records to AgentTrigger (cron type).

Run this script once after deploying Phase 2 of the Aware engine.
It converts all existing agent_schedules into agent_triggers with type='cron'.

Usage:
    python -m app.scripts.migrate_schedules_to_triggers
"""

import asyncio

from app.core.logging import logger
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.db.pool import close_pool, init_pool


async def migrate():
    """Convert all AgentSchedule records to AgentTrigger(type='cron')."""
    _ = await init_pool()
    try:
        schedules = await agent_schedule_dao.list_all()

        if not schedules:
            logger.info("No schedules found to migrate.")
            return

        migrated = 0
        skipped = 0
        for s in schedules:
            trigger_name = f"migrated_{s.name[:80]}"
            existing = await agent_trigger_dao.get_by_agent_and_name(s.agent_id, trigger_name)
            if existing:
                logger.info(f"  Skip: '{s.name}' already migrated")
                skipped += 1
                continue

            _ = await agent_trigger_dao.create(
                obj_in={
                    "agent_id": s.agent_id,
                    "name": trigger_name,
                    "type": "cron",
                    "config": {"expr": s.cron_expr},
                    "reason": s.instruction[:500] if s.instruction else f"Migrated schedule: {s.name}",
                    "is_enabled": s.is_enabled,
                    "fire_count": s.run_count or 0,
                    "last_fired_at": s.last_run_at,
                }
            )
            # Disable the source schedule so it won't be re-migrated
            _ = await agent_schedule_dao.update(db_obj=s, obj_in={"is_enabled": False})
            migrated += 1
            logger.info(f"  Migrated: '{s.name}' -> cron({s.cron_expr})")

        logger.info(f"Migration complete: {migrated} migrated, {skipped} skipped")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(migrate())
