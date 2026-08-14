"""Supervision reminder service - periodically sends reminders for supervision tasks.

Checks all supervision-type tasks that are not done and sends Feishu reminders
to the target person based on the configured schedule preset.

Schedule presets: daily, every_2_days, every_3_days, weekly

Runs as a background task inside the FastAPI process.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, NotRequired, TypedDict

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.llm_dao import llm_model_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.participant_dao import participant_dao
from app.dao.task_dao import task_dao, task_log_dao
from app.records.task import TaskRecord
from app.services.activity_logger import log_activity
from app.records.agent import AgentRecord

# Schedule JSON format:
# {"freq": "daily"|"weekly", "interval": N, "time": "HH:MM", "weekdays": [0-6]}
# weekdays: 0=Sun, 1=Mon, ..., 6=Sat


class ReminderSchedule(TypedDict):
    freq: str
    interval: int
    time: str
    weekdays: NotRequired[list[int]]


def _parse_schedule(remind_schedule: str) -> ReminderSchedule | None:
    """Parse remind_schedule - supports JSON format or legacy simple presets."""
    import json

    if not remind_schedule:
        return None
    try:
        sched = json.loads(remind_schedule)
        if isinstance(sched, dict):
            sched_map = dict[str, object](sched)
            frequency = sched_map.get("freq")
            interval = sched_map.get("interval")
            time_of_day = sched_map.get("time")
            weekdays = sched_map.get("weekdays")
            if (
                isinstance(frequency, str)
                and frequency in {"daily", "weekly"}
                and isinstance(interval, int)
                and not isinstance(interval, bool)
                and interval > 0
                and isinstance(time_of_day, str)
                and (
                    weekdays is None
                    or (isinstance(weekdays, list) and all(isinstance(day, int) for day in list[object](weekdays)))
                )
            ):
                schedule: ReminderSchedule = {
                    "freq": frequency,
                    "interval": interval,
                    "time": time_of_day,
                }
                if weekdays is not None:
                    schedule["weekdays"] = weekdays
                return schedule
    except json.JSONDecodeError, TypeError:
        pass
    # Legacy simple preset fallback
    legacy_map: dict[str, ReminderSchedule] = {
        "daily": {"freq": "daily", "interval": 1, "time": "09:00"},
        "every_2_days": {"freq": "daily", "interval": 2, "time": "09:00"},
        "every_3_days": {"freq": "daily", "interval": 3, "time": "09:00"},
        "weekly": {"freq": "weekly", "interval": 1, "time": "09:00", "weekdays": [1, 2, 3, 4, 5]},
    }
    return legacy_map.get(remind_schedule)


def _is_reminder_due(remind_schedule: str, last_reminded_at: datetime | None, now_utc: datetime) -> bool:
    """Check if a reminder is due based on the schedule config.

    All time calculations are anchored to now_utc (provided by tick loop).
    Default behavior is to use UTC for hour/minute checks unless a timezone is specified.
    """
    sched = _parse_schedule(remind_schedule)
    if not sched:
        return False

    freq = sched["freq"]
    interval = sched["interval"]
    time_str = sched["time"]

    # Parse target hour/minute
    try:
        th, tm = map(int, time_str.split(":"))
    except Exception:
        th, tm = 9, 0

    # For now, we use UTC for the hour/minute check.
    # In the future, we should load agent.timezone and convert now_utc.
    current_time = now_utc

    # Not yet time today
    if current_time.hour < th or (current_time.hour == th and current_time.minute < tm):
        return False

    # Already past the time window (allow 60-min window)
    if current_time.hour > th or (current_time.hour == th and current_time.minute > tm + 59):
        return False

    # Weekly: check if today is a selected weekday
    if freq == "weekly":
        weekdays = sched.get("weekdays", [1, 2, 3, 4, 5])
        # Python: Monday=0, Sunday=6 → convert to our format: Sunday=0, Monday=1, ...
        py_weekday = current_time.weekday()  # Mon=0
        our_weekday = (py_weekday + 1) % 7  # Sun=0
        if our_weekday not in weekdays:
            return False

    # Check interval since last reminder
    if last_reminded_at is None:
        return True

    # Ensure both are timezone-aware for comparison
    if last_reminded_at.tzinfo is None:
        last_reminded_at = last_reminded_at.replace(tzinfo=UTC)

    elapsed = now_utc - last_reminded_at
    min_interval = timedelta(days=interval) - timedelta(hours=2)  # tolerance
    return elapsed >= min_interval


async def _get_agent_reply(target_agent: AgentRecord, message: str) -> str | None:
    """Call target agent's LLM to generate a reply to a supervision reminder.

    Returns the reply text, or None if the agent can't respond.
    """
    from app.services.agent_context import build_agent_context
    from app.services.llm import (
        LLMError,
        LLMMessage,
        create_llm_client,
        get_model_api_key,
        get_provider_base_url,
    )

    model_id = target_agent.primary_model_id or target_agent.fallback_model_id
    if not model_id:
        return None

    model = await llm_model_dao.get(model_id)
    if not model:
        return None

    base_url = get_provider_base_url(model.provider, model.base_url)
    if not base_url:
        return None

    static_prompt, dynamic_prompt = await build_agent_context(
        target_agent.id, target_agent.name, target_agent.role_description or ""
    )

    messages = [
        LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt),
        LLMMessage(role="user", content=message),
    ]

    client = create_llm_client(
        provider=model.provider,
        api_key=get_model_api_key(model),
        model=model.model,
        base_url=base_url,
        timeout=float(getattr(model, "request_timeout", None) or 60.0),
    )
    try:
        response = await client.complete(
            messages=messages,
            temperature=model.temperature,
            max_tokens=512,
        )
        content = (response.content or "").strip()
        return content if content else None
    except LLMError as e:
        logger.error(f"_get_agent_reply LLM error: {e}")
    except Exception as e:
        logger.error(f"_get_agent_reply LLM call failed: {e}")
    finally:
        await client.close()
    return None


async def _send_supervision_reminder(task: TaskRecord, agent_name: str):
    """Send a single supervision reminder. Target can be an Agent or a Member."""
    try:
        import json as _json

        from app.services.feishu_service import feishu_service

        target_name = task.supervision_target_name
        if not target_name:
            logger.warning(f"Supervision task {task.id} has no target name")
            return

        created_at = task.created_at or datetime.now(UTC)
        days_since = (datetime.now(UTC) - created_at).days
        reminder_msg = f"📋 Supervision reminder from {agent_name}\n\nTask: {task.title}\n"
        if task.description:
            reminder_msg += f"Details: {task.description}\n"
        reminder_msg += f"Created: {days_since} days ago\n"
        if task.due_date:
            reminder_msg += f"Due date: {task.due_date.strftime('%Y-%m-%d')}\n"
        reminder_msg += "\nPlease handle this promptly. Thank you!"

        sent = False
        send_method = ""
        target_agent = await agent_dao.get_by_name(target_name)

        if target_agent:
            src_part = await participant_dao.get_by_type_ref("agent", task.agent_id)
            tgt_part = await participant_dao.get_by_type_ref("agent", target_agent.id)

            session_agent_id = min(task.agent_id, target_agent.id, key=str)
            session_peer_id = max(task.agent_id, target_agent.id, key=str)
            chat_session = await chat_session_dao.get_agent_peer_session(
                session_agent_id=session_agent_id,
                peer_agent_id=session_peer_id,
            )
            if not chat_session:
                src_agent = await agent_dao.get(task.agent_id)
                owner_id = src_agent.creator_id if src_agent else task.agent_id
                chat_session = await chat_session_dao.create(
                    obj_in={
                        "agent_id": session_agent_id,
                        "user_id": owner_id,
                        "title": f"{agent_name} ↔ {target_agent.name}",
                        "source_channel": "agent",
                        "participant_id": src_part.id if src_part else None,
                        "peer_agent_id": session_peer_id,
                    }
                )

            session_id = str(chat_session.id)
            src_agent2 = await agent_dao.get(task.agent_id)
            owner_id = src_agent2.creator_id if src_agent2 else task.agent_id

            _ = await chat_message_dao.insert_message(
                agent_id=session_agent_id,
                user_id=owner_id,
                role="user",
                content=reminder_msg,
                conversation_id=session_id,
                participant_id=src_part.id if src_part else None,
            )
            _ = await chat_session_dao.update(
                db_obj=chat_session,
                obj_in={"last_message_at": datetime.now(UTC)},
            )
            sent = True
            send_method = "agent message"

            try:
                reply = await _get_agent_reply(target_agent, reminder_msg)
                if reply:
                    _ = await chat_message_dao.insert_message(
                        agent_id=session_agent_id,
                        user_id=owner_id,
                        role="assistant",
                        content=reply,
                        conversation_id=session_id,
                        participant_id=tgt_part.id if tgt_part else None,
                    )
                    send_method = f"agent message + reply({reply[:40]})"
                    logger.info(f"📋 Target agent {target_agent.name} replied: {reply[:80]}")
            except Exception as e:
                logger.warning(f"Target agent reply failed: {e}")
        else:
            target_member = await org_member_dao.find_related_to_agent_by_name(task.agent_id, target_name)
            if target_member:
                config = await channel_config_dao.get_for_agent(agent_id=task.agent_id, channel_type="feishu")
                if config and config.app_id and config.app_secret and (target_member.email or target_member.phone):
                    try:
                        resolved = await feishu_service.resolve_open_id(
                            config.app_id,
                            config.app_secret,
                            email=target_member.email,
                            mobile=target_member.phone,
                        )
                        if resolved:
                            content = _json.dumps({"text": reminder_msg}, ensure_ascii=False)
                            resp = await feishu_service.send_message(
                                config.app_id,
                                config.app_secret,
                                receive_id=resolved,
                                msg_type="text",
                                content=content,
                                receive_id_type="open_id",
                            )
                            if resp.get("code") == 0:
                                sent = True
                                send_method = "Feishu"
                    except Exception as error:
                        logger.warning(f"Failed to send Feishu supervision reminder to {target_name}: {error}")

        if sent:
            log_content = f"✅ Sent supervision reminder to {target_name} ({send_method})"
        elif target_agent or target_name:
            log_content = f"📋 Supervision reminder triggered, target: {target_name}"
        else:
            log_content = f"⚠️ Reminder failed: contact '{target_name}' was not found"
        _ = await task_log_dao.create(obj_in={"task_id": task.id, "content": log_content})

        await log_activity(
            task.agent_id,
            "schedule_run",
            f"📋 Supervision reminder: {task.title} → {target_name}" + (f" (sent via {send_method})" if sent else ""),
            detail={"task_id": str(task.id), "target": target_name, "sent": sent},
            related_id=task.id,
        )

        logger.info(f"📋 Supervision reminder for '{task.title}' -> {target_name}, sent={sent}")

    except Exception as e:
        logger.exception(f"Supervision reminder error for task {task.id}: {e}")


async def _supervision_tick():
    """One tick: check all supervision tasks and send due reminders."""
    logger.info("[supervision] tick running...")
    from app.services.audit_logger import write_audit_log

    try:
        now = datetime.now(UTC)
        rows = await task_dao.list_active_supervision()
        logger.info(f"[supervision] found {len(rows)} supervision tasks")

        await write_audit_log("supervision_tick", {"tasks_found": len(rows)})

        for task, agent_name in rows:
            try:
                last_log = await task_log_dao.latest_for_task(task.id)
                last_reminded = last_log.created_at if last_log else None

                if task.remind_schedule and _is_reminder_due(task.remind_schedule, last_reminded, now):
                    logger.info(f"[supervision] FIRING reminder for '{task.title}' -> {task.supervision_target_name}")
                    await write_audit_log(
                        "supervision_fire",
                        {"task_id": str(task.id), "title": task.title, "target": task.supervision_target_name},
                        agent_id=task.agent_id,
                    )
                    await _send_supervision_reminder(task, agent_name)

            except Exception as e:
                logger.error(f"Error checking supervision task {task.id}: {e}")

    except Exception as e:
        logger.exception(f"Supervision tick error: {e}")
        await write_audit_log("supervision_error", {"error": str(e)[:300]})


async def start_supervision_reminder():
    """Start the background supervision reminder loop. Call from FastAPI startup."""
    logger.info("📋 [supervision] Reminder service started (60s tick)")
    logger.info("📋 Supervision reminder service started (60s tick)")
    while True:
        await _supervision_tick()
        await asyncio.sleep(60)
