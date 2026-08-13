"""Trigger evaluation and deterministic special-case handlers."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from croniter import croniter

from app.core.json_types import JsonValue
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.participant_dao import participant_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.db.session import connection_ctx

MIN_POLL_INTERVAL_MINUTES = 5


def _config_number(value: JsonValue) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def _config_headers(value: JsonValue) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None

    headers: dict[str, str] = {}
    for key, header_value in value.items():
        if not isinstance(header_value, str):
            return None
        headers[key] = header_value
    return headers


async def should_skip_non_workday(trigger: Any, local_now: datetime) -> bool:
    if trigger.name != "daily_okr_collection":
        return False

    from app.services.business_calendar import is_non_workday

    agent = await agent_dao.get(trigger.agent_id)
    if not agent or not agent.tenant_id:
        return False

    tenant_id = agent.tenant_id
    async with connection_ctx() as db:
        skip_enabled = await db.fetchval(
            "SELECT daily_report_skip_non_workdays FROM okr_settings WHERE tenant_id = %(tenant_id)s",
            {"tenant_id": tenant_id},
        )
    if skip_enabled is False:
        return False

    tenant = await tenant_dao.get(tenant_id)
    country_region = tenant.country_region if tenant else None
    return is_non_workday(local_now.date(), country_region)


async def mark_trigger_skipped(trigger_id: uuid.UUID, now: datetime) -> None:
    try:
        async with connection_ctx() as db:
            await db.execute(
                "UPDATE agent_triggers SET last_fired_at = %(now)s WHERE id = %(id)s",
                {"now": now, "id": trigger_id},
            )
    except Exception as e:
        logger.warning(f"Failed to mark skipped trigger {trigger_id}: {e}")


async def mark_trigger_fired(trigger_id: uuid.UUID, now: datetime) -> None:
    try:
        async with connection_ctx() as db:
            await db.execute(
                """
                UPDATE agent_triggers
                SET last_fired_at = %(now)s,
                    fire_count = fire_count + 1,
                    is_enabled = CASE
                        WHEN type = 'once' THEN FALSE
                        WHEN max_fires IS NOT NULL AND fire_count + 1 >= max_fires THEN FALSE
                        ELSE is_enabled
                    END
                WHERE id = %(id)s
                """,
                {"now": now, "id": trigger_id},
            )
    except Exception as e:
        logger.warning(f"Failed to mark fired trigger {trigger_id}: {e}")


async def handle_okr_report_trigger(trigger: Any, now: datetime) -> bool:
    if trigger.name not in {"daily_okr_report", "weekly_okr_report", "monthly_okr_report"}:
        return False

    from zoneinfo import ZoneInfo

    from app.services.okr_reporting import (
        generate_company_daily_report,
        generate_company_monthly_report,
        generate_company_weekly_report,
    )
    from app.services.timezone_utils import get_agent_timezone

    agent = await agent_dao.get(trigger.agent_id)
    if not agent or not agent.tenant_id:
        return True

    tenant_id = agent.tenant_id
    async with connection_ctx() as db:
        settings = await db.fetchone(
            "SELECT enabled FROM okr_settings WHERE tenant_id = %(tenant_id)s",
            {"tenant_id": tenant_id},
        )
    if not settings or not settings.get("enabled"):
        return True

    tz_name = await get_agent_timezone(trigger.agent_id)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    local_today = now.astimezone(tz).date()

    if trigger.name == "daily_okr_report":
        await generate_company_daily_report(tenant_id, local_today - timedelta(days=1))
    elif trigger.name == "weekly_okr_report":
        previous_week_anchor = local_today - timedelta(days=7)
        week_start = previous_week_anchor - timedelta(days=previous_week_anchor.weekday())
        await generate_company_weekly_report(tenant_id, week_start)
    elif trigger.name == "monthly_okr_report":
        previous_month_end = local_today.replace(day=1) - timedelta(days=1)
        await generate_company_monthly_report(tenant_id, previous_month_end)

    await mark_trigger_fired(trigger.id, now)
    logger.info(f"[Trigger] Auto-generated OKR report for trigger {trigger.name}")
    return True


async def handle_okr_collection_trigger(trigger: Any, now: datetime) -> bool:
    if trigger.name != "daily_okr_collection":
        return False

    from app.services.okr_daily_collection import trigger_daily_collection_for_tenant

    agent = await agent_dao.get(trigger.agent_id)
    if not agent or not agent.tenant_id:
        return True

    tenant_id = agent.tenant_id
    async with connection_ctx() as db:
        settings = await db.fetchone(
            "SELECT enabled, daily_report_enabled FROM okr_settings WHERE tenant_id = %(tenant_id)s",
            {"tenant_id": tenant_id},
        )
    if not settings or not settings.get("enabled") or not settings.get("daily_report_enabled"):
        return True

    await trigger_daily_collection_for_tenant(tenant_id)
    await mark_trigger_fired(trigger.id, now)
    logger.info(f"[Trigger] Deterministic OKR collection sent for trigger {trigger.name}")
    return True


def is_private_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):  # noqa: S104 - blocks unspecified-interface targets at the SSRF boundary
            return True
        import socket

        try:
            infos = socket.getaddrinfo(hostname, None)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
        except socket.gaierror, ValueError:
            return True
        return False
    except Exception:
        return True


async def evaluate_trigger(trigger: Any, now: datetime) -> bool:
    if not trigger.is_enabled:
        return False
    if trigger.expires_at and now >= trigger.expires_at:
        return False
    if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
        return False

    if trigger.last_fired_at:
        cooldown = timedelta(seconds=trigger.cooldown_seconds)
        if (now - trigger.last_fired_at) < cooldown:
            return False

    cfg = trigger.config or {}
    t = trigger.type

    if t == "cron":
        expr = cfg.get("expr", "* * * * *")
        if not isinstance(expr, str):
            return False
        base = trigger.last_fired_at or trigger.created_at
        try:
            configured_timezone = cfg.get("timezone")
            if isinstance(configured_timezone, str) and configured_timezone:
                tz_name = configured_timezone
            else:
                from app.services.timezone_utils import get_agent_timezone

                tz_name = await get_agent_timezone(trigger.agent_id)
            from zoneinfo import ZoneInfo

            try:
                tz = ZoneInfo(tz_name)
            except KeyError, Exception:
                tz = ZoneInfo("UTC")
            local_now = now.astimezone(tz)
            local_base = base.astimezone(tz) if base.tzinfo else base.replace(tzinfo=tz)
            cron = croniter(expr, local_base)
            next_run = cron.get_next(datetime)
            if local_now >= next_run:
                if await should_skip_non_workday(trigger, local_now):
                    await mark_trigger_skipped(trigger.id, now)
                    logger.info(f"[Trigger] Skipped {trigger.name} on non-workday {local_now.date()}")
                    return False
                return True
            return False
        except Exception as e:
            logger.warning(f"Invalid cron expr '{expr}' for trigger {trigger.name}: {e}")
            return False

    if t == "once":
        at_str = cfg.get("at")
        if not isinstance(at_str, str) or not at_str:
            return False
        try:
            at = datetime.fromisoformat(at_str)
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            return now >= at and trigger.fire_count == 0
        except Exception:
            return False

    if t == "interval":
        minutes = _config_number(cfg.get("minutes", 30))
        if minutes is None:
            return False
        base = trigger.last_fired_at or trigger.created_at
        return (now - base) >= timedelta(minutes=minutes)

    if t == "poll":
        configured_interval = _config_number(cfg.get("interval_min", 5))
        if configured_interval is None:
            return False
        interval_min = max(configured_interval, MIN_POLL_INTERVAL_MINUTES)
        base = trigger.last_fired_at or trigger.created_at
        if (now - base) < timedelta(minutes=interval_min):
            return False
        return await poll_check(trigger)

    if t == "on_message":
        return await check_new_agent_messages(trigger)

    if t == "webhook":
        return False

    return False


async def poll_check(trigger: Any) -> bool:
    import httpx

    cfg = trigger.config or {}
    url = cfg.get("url")
    if not isinstance(url, str) or not url:
        return False
    if is_private_url(url):
        logger.warning(f"Poll blocked for trigger {trigger.name}: private/internal URL '{url}'")
        return False
    try:
        method = cfg.get("method", "GET")
        if not isinstance(method, str):
            return False
        headers = _config_headers(cfg.get("headers", {}))
        if headers is None:
            return False
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(method, url, headers=headers)
            resp.raise_for_status()

        data = resp.json()
        json_path = cfg.get("json_path", "$")
        if not isinstance(json_path, str):
            return False
        current_value = extract_json_path(data, json_path)
        current_str = str(current_value)
        fire_on = cfg.get("fire_on", "change")
        should_fire = False
        if fire_on == "match":
            should_fire = current_str == str(cfg.get("match_value", ""))
        else:
            last_value = cfg.get("_last_value")
            should_fire = last_value is not None and current_str != last_value

        cfg["_last_value"] = current_str
        try:
            row = await agent_trigger_dao.get(trigger.id)
            if row:
                await agent_trigger_dao.update(db_obj=row, obj_in={"config": cfg})
        except Exception as e:
            logger.warning(f"Failed to persist poll _last_value for {trigger.name}: {e}")

        return should_fire
    except Exception as e:
        logger.warning(f"Poll failed for trigger {trigger.name}: {e}")
        return False


def extract_json_path(data, path: str):
    if path == "$" or not path:
        return data
    parts = path.lstrip("$.").split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


async def check_new_agent_messages(trigger: Any) -> bool:
    cfg = trigger.config or {}
    from_agent_name = cfg.get("from_agent_name")
    from_user_name = cfg.get("from_user_name")
    if not from_agent_name and not from_user_name:
        return False

    since = trigger.last_fired_at or trigger.created_at
    if trigger.fire_count == 0 and not trigger.last_fired_at:
        since_ts_str = cfg.get("_since_ts")
        if isinstance(since_ts_str, str) and since_ts_str:
            try:
                since = datetime.fromisoformat(since_ts_str)
            except Exception:
                since = trigger.created_at

    try:
        if from_agent_name:
            if isinstance(from_agent_name, list):
                from_agent_name = from_agent_name[0] if from_agent_name else ""
            if not isinstance(from_agent_name, str):
                return False
            safe_agent_name = from_agent_name.replace("%", "").replace("_", r"\_")
            async with connection_ctx() as db:
                source_agent = await db.fetchone(
                    "SELECT id FROM agents WHERE name ILIKE %(pattern)s LIMIT 1",
                    {"pattern": f"%{safe_agent_name}%"},
                )
            if not source_agent:
                return False

            from_participant = await participant_dao.get_by_type_ref("agent", source_agent["id"])
            if not from_participant:
                return False

            async with connection_ctx() as db:
                msg = await db.fetchone(
                    """
                    SELECT cm.content
                    FROM chat_messages cm
                    JOIN chat_sessions cs ON cm.conversation_id = cs.id::text
                    WHERE cm.participant_id = %(participant_id)s
                      AND cm.created_at > %(since)s
                      AND cm.role = ANY(%(roles)s)
                      AND cs.source_channel <> 'trigger'
                    ORDER BY cm.created_at DESC
                    LIMIT 1
                    """,
                    {
                        "participant_id": from_participant.id,
                        "since": since,
                        "roles": ["assistant", "user"],
                    },
                )
            if not msg:
                return False
            cfg["_matched_message"] = (msg.get("content") or "")[:2000]
            cfg["_matched_from"] = from_agent_name
            return True

        if from_user_name:
            agent = await agent_dao.get(trigger.agent_id)
            if isinstance(from_user_name, list):
                from_user_name = from_user_name[0] if from_user_name else ""
            if not isinstance(from_user_name, str):
                return False
            safe_user_name = from_user_name.replace("%", "").replace("_", r"\_")
            pattern = f"%{safe_user_name}%"

            async with connection_ctx() as db:
                if agent and agent.tenant_id:
                    target_user = await db.fetchone(
                        """
                        SELECT u.id
                        FROM users u
                        JOIN identities i ON i.id = u.identity_id
                        WHERE (
                            u.display_name ILIKE %(pattern)s
                            OR i.username ILIKE %(pattern)s
                        )
                          AND u.tenant_id = %(tenant_id)s
                        LIMIT 1
                        """,
                        {"pattern": pattern, "tenant_id": agent.tenant_id},
                    )
                else:
                    target_user = await db.fetchone(
                        """
                        SELECT u.id
                        FROM users u
                        JOIN identities i ON i.id = u.identity_id
                        WHERE (
                            u.display_name ILIKE %(pattern)s
                            OR i.username ILIKE %(pattern)s
                        )
                        LIMIT 1
                        """,
                        {"pattern": pattern},
                    )

                if target_user:
                    msg = await db.fetchone(
                        """
                        SELECT cm.content
                        FROM chat_messages cm
                        JOIN chat_sessions cs ON cm.conversation_id = cs.id::text
                        WHERE cs.agent_id = %(agent_id)s
                          AND cs.user_id = %(user_id)s
                          AND cs.source_channel = ANY(%(channels)s)
                          AND cm.role = 'user'
                          AND cm.created_at > %(since)s
                        ORDER BY cm.created_at DESC
                        LIMIT 1
                        """,
                        {
                            "agent_id": trigger.agent_id,
                            "user_id": target_user["id"],
                            "channels": ["feishu", "slack", "discord", "web"],
                            "since": since,
                        },
                    )
                else:
                    msg = await db.fetchone(
                        """
                        SELECT cm.content
                        FROM chat_messages cm
                        JOIN chat_sessions cs ON cm.conversation_id = cs.id::text
                        WHERE cs.agent_id = %(agent_id)s
                          AND cs.source_channel = ANY(%(channels)s)
                          AND cm.role = 'user'
                          AND cm.created_at > %(since)s
                          AND (
                            cs.title ILIKE %(pattern)s
                            OR cm.content ILIKE %(pattern)s
                          )
                        ORDER BY cm.created_at DESC
                        LIMIT 1
                        """,
                        {
                            "agent_id": trigger.agent_id,
                            "channels": ["feishu", "slack", "discord", "web"],
                            "since": since,
                            "pattern": pattern,
                        },
                    )

            if not msg:
                return False
            cfg["_matched_message"] = (msg.get("content") or "")[:2000]
            cfg["_matched_from"] = from_user_name
            return True
    except Exception as e:
        logger.warning(f"on_message check failed for trigger {trigger.name}: {e}")
        return False

    return False
