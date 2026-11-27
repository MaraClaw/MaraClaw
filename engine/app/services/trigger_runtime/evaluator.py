"""Trigger evaluation and deterministic special-case handlers."""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeIs
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.core.json_types import (
    JsonObject,
    JsonValue,
    is_any_list,
    is_json_object,
    json_value_from_response,
    str_from_row,
    uuid_from_row,
)
from app.core.logging import logger
from app.core.outbound_policy import OutboundPolicyError, validate_public_url
from app.core.outbound_transport import public_http_client
from app.core.trigger_schedule import (
    MIN_POLL_INTERVAL_MINUTES,
    config_number,
    configured_timezone,
    cron_next_run,
    poll_checkpoint,
    poll_reference,
    zone_info,
)
from app.dao.agent_dao import agent_dao
from app.dao.participant_dao import participant_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.db.session import connection_ctx
from app.records.trigger import AgentTriggerRecord
from app.services.trigger_runtime.keys import POLL_OBSERVED_KEY, poll_sequence


@dataclass(frozen=True, slots=True)
class PollObservation:
    """One poll result that differs from the stored baseline.

    ``expected_seq`` is the sequence the baseline write must still find; ``fire`` says whether the observation
    also warrants an execution (a first poll or a non-entering match only moves the baseline).
    """

    value: str
    expected_seq: int
    fire: bool


def _is_dict(value: object) -> TypeIs[dict[object, object]]:
    return isinstance(value, dict)


def _is_json_value(value: object) -> TypeIs[JsonValue]:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if is_any_list(value):
        return all(_is_json_value(item) for item in value)
    if _is_dict(value):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _json_config(value: object) -> JsonObject:
    if is_json_object(value):
        return value
    if not _is_dict(value):
        return {}
    result: JsonObject = {}
    for key, item in dict[object, object](value).items():
        if isinstance(key, str) and _is_json_value(item):
            result[key] = item
    return result


def _config_headers(value: JsonValue) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None

    headers: dict[str, str] = {}
    for key, header_value in value.items():
        if not isinstance(header_value, str):
            return None
        headers[key] = header_value
    return headers


async def should_skip_non_workday(trigger: AgentTriggerRecord, local_now: datetime) -> bool:
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
        await agent_trigger_dao.mark_skipped(trigger_id, now)
    except Exception as e:
        logger.warning(f"Failed to mark skipped trigger {trigger_id}: {e}")


def is_private_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if hostname in (None, ""):
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


async def schedule_timezone(trigger: AgentTriggerRecord, agent_timezone: str | None = None) -> ZoneInfo:
    """The zone a trigger's schedule is evaluated in: its configured timezone, else its agent's, else UTC."""
    tz_name = configured_timezone(_json_config(trigger.config))
    if tz_name is None and agent_timezone is not None:
        tz_name = agent_timezone
    if tz_name is None:
        from app.services.timezone_utils import get_agent_timezone

        tz_name = await get_agent_timezone(trigger.agent_id)
    return zone_info(tz_name)


async def evaluate_trigger(trigger: AgentTriggerRecord, now: datetime, *, agent_timezone: str | None = None) -> bool:
    """Whether the trigger is due. ``agent_timezone`` is a timezone the caller resolved for the whole tick."""
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

    cfg = _json_config(trigger.config)
    t = trigger.type

    if t == "cron":
        expr = cfg.get("expr", "* * * * *")
        if not isinstance(expr, str):
            return False
        base = trigger.last_fired_at or trigger.created_at
        try:
            tz = await schedule_timezone(trigger, agent_timezone)
            if base is None:
                return False
            local_now = now.astimezone(tz)
            next_run = cron_next_run(expr, base, tz)
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
        minutes = config_number(cfg.get("minutes", 30))
        if minutes is None:
            return False
        base = trigger.last_fired_at or trigger.created_at
        if base is None:
            return False
        return (now - base) >= timedelta(minutes=minutes)

    if t == "poll":
        configured_interval = config_number(cfg.get("interval_min", 5))
        if configured_interval is None:
            return False
        interval_min = max(configured_interval, MIN_POLL_INTERVAL_MINUTES)
        checkpoint = poll_checkpoint(cfg)
        base = poll_reference(trigger, checkpoint)
        if base is None or (now - base) < timedelta(minutes=interval_min):
            return False
        # One winner per interval across replicas; a failed or unchanged poll waits for the next interval too.
        if not await agent_trigger_dao.claim_poll_checkpoint(
            trigger.id, seen=checkpoint, expected_seq=poll_sequence(cfg), claimed_at=now
        ):
            return False
        observation = await poll_check(trigger)
        if observation is None:
            return False
        if not observation.fire:
            # Baseline-only change: conditional, so a concurrent worker or config edit is never overwritten.
            _ = await agent_trigger_dao.advance_poll_baseline(
                trigger.id, expected_seq=observation.expected_seq, value=observation.value
            )
            return False
        # Handed to `enqueue_due_trigger`, which advances the baseline in the transaction that enqueues.
        trigger.config[POLL_OBSERVED_KEY] = observation.value
        return True

    if t == "on_message":
        return await check_new_agent_messages(trigger) is not None

    if t == "webhook":
        return False

    return False


async def poll_check(trigger: AgentTriggerRecord) -> PollObservation | None:
    """Fetch the poll target and compare it with the stored baseline; never writes config.

    Returns None when nothing needs recording. A first poll yields a baseline-only observation. With
    ``fire_on=match`` an execution is warranted only when the value enters the match value.
    """
    cfg = _json_config(trigger.config)
    url = cfg.get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        url = await validate_public_url(url)
        method = cfg.get("method", "GET")
        if not isinstance(method, str):
            return None
        headers = _config_headers(cfg.get("headers", {}))
        if headers is None:
            return None
        async with public_http_client(timeout=10, follow_redirects=False) as client:
            resp = await client.request(method, url, headers=headers)
            _ = resp.raise_for_status()

        data = json_value_from_response(resp)
        json_path = cfg.get("json_path", "$")
        if not isinstance(json_path, str):
            return None
        current_str = str(extract_json_path(data, json_path))
        previous = cfg.get("_last_value")
        previous = previous if isinstance(previous, str) else None
        if previous == current_str:
            return None

        rule_met = cfg.get("fire_on", "change") != "match" or current_str == str(cfg.get("match_value", ""))
        fire = previous is not None and rule_met
        return PollObservation(value=current_str, expected_seq=poll_sequence(cfg), fire=fire)
    except OutboundPolicyError as exc:
        logger.warning("Poll blocked by outbound policy for trigger {}: {}", trigger.id, exc)
        return None
    except Exception as e:
        logger.warning(f"Poll failed for trigger {trigger.name}: {e}")
        return None


def extract_json_path(data: object, path: str) -> object:
    if path == "$" or not path:
        return data
    parts = path.lstrip("$.").split(".")
    current: object = data
    for part in parts:
        if _is_dict(current):
            current = current.get(part)
        elif is_any_list(current) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


async def check_new_agent_messages(trigger: AgentTriggerRecord) -> str | None:
    """Return the id of the newest matching chat message, or None; the match is recorded on the trigger config."""
    cfg = _json_config(trigger.config)
    from_agent_name = cfg.get("from_agent_name")
    from_user_name = cfg.get("from_user_name")
    if (from_agent_name is None or not from_agent_name) and (from_user_name is None or not from_user_name):
        return None

    since = trigger.last_fired_at or trigger.created_at
    if trigger.fire_count == 0 and not trigger.last_fired_at:
        since_ts_str = cfg.get("_since_ts")
        if isinstance(since_ts_str, str) and since_ts_str:
            try:
                since = datetime.fromisoformat(since_ts_str)
            except Exception:
                since = trigger.created_at

    try:
        if from_agent_name is not None and from_agent_name:
            if isinstance(from_agent_name, list):
                from_agent_name = from_agent_name[0] if from_agent_name else ""
            if not isinstance(from_agent_name, str):
                return None
            safe_agent_name = from_agent_name.replace("%", "").replace("_", r"\_")
            async with connection_ctx() as db:
                # Only agents in the trigger agent's own tenant can be named; a tenantless trigger agent matches none.
                source_agent = await db.fetchone(
                    """
                    SELECT a.id
                    FROM agents a
                    JOIN agents ta ON ta.id = %(trigger_agent_id)s
                    WHERE a.name ILIKE %(pattern)s
                      AND a.tenant_id = ta.tenant_id
                    LIMIT 1
                    """,
                    {"pattern": f"%{safe_agent_name}%", "trigger_agent_id": trigger.agent_id},
                )
            if source_agent is None:
                return None

            from_participant = await participant_dao.get_by_type_ref("agent", uuid_from_row(source_agent["id"]))
            if not from_participant:
                return None

            async with connection_ctx() as db:
                msg = await db.fetchone(
                    """
                    SELECT cm.id, cm.content
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
            if msg is None:
                return None
            return _record_matched_message(cfg, msg, from_agent_name)

        if from_user_name is not None and from_user_name:
            agent = await agent_dao.get(trigger.agent_id)
            if isinstance(from_user_name, list):
                from_user_name = from_user_name[0] if from_user_name else ""
            if not isinstance(from_user_name, str):
                return None
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
                            OR i.email ILIKE %(pattern)s
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
                            OR i.email ILIKE %(pattern)s
                        )
                        LIMIT 1
                        """,
                        {"pattern": pattern},
                    )

                if target_user is not None:
                    msg = await db.fetchone(
                        """
                        SELECT cm.id, cm.content
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
                        SELECT cm.id, cm.content
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

            if msg is None:
                return None
            return _record_matched_message(cfg, msg, from_user_name)
    except Exception as e:
        logger.warning(f"on_message check failed for trigger {trigger.name}: {e}")
        return None

    return None


def _record_matched_message(cfg: JsonObject, msg: Mapping[str, object], matched_from: str) -> str:
    message_id = str_from_row(msg.get("id"))
    cfg["_matched_message"] = str_from_row(msg.get("content"))[:2000]
    cfg["_matched_message_id"] = message_id
    cfg["_matched_from"] = matched_from
    return message_id
