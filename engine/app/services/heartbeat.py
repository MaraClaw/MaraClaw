"""Heartbeat service - proactive agent awareness loop.

Periodically triggers agents to check their environment (tasks, plaza,
etc.) and take autonomous actions. Inspired by OpenClaw's heartbeat
mechanism.

Runs as a background task inside the FastAPI process.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import logger, new_trace_id
from app.services.llm.finish import FINISH_PROTOCOL_REMINDER, find_finish_call, parse_tool_arguments
from app.services.llm.types import LLMToolCall, LLMToolFunction, ToolPayload
from app.services.storage import agent_storage_key, get_storage_backend

_HEARTBEAT_SEMAPHORE = asyncio.Semaphore(10)
_HEARTBEAT_TASKS: set[asyncio.Task[None]] = set()


def _tool_argument_preview(arguments: str | ToolPayload, length: int) -> str:
    if isinstance(arguments, str):
        return arguments[:length]
    return repr(arguments)[:length]


def _tool_call_parts(tool_call: LLMToolCall) -> tuple[str, str, LLMToolFunction]:
    tool_call_id = tool_call.get("id")
    function = tool_call.get("function")
    tool_name = function.get("name") if function is not None else None
    if not isinstance(tool_call_id, str):
        raise KeyError("Tool call is missing a string id")
    if function is None:
        raise KeyError("Tool call is missing a function payload")
    if not isinstance(tool_name, str):
        raise KeyError("Tool call function is missing a string name")
    return tool_call_id, tool_name, function


def _observe_heartbeat_task(task: asyncio.Task[None]) -> None:
    _HEARTBEAT_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("Heartbeat task failed")


# Default heartbeat instruction used when HEARTBEAT.md doesn't exist
DEFAULT_HEARTBEAT_INSTRUCTION = """[Heartbeat Check]

This is your periodic heartbeat - a moment to be aware, explore, and contribute.

## Phase 1: Review Context & Discover Interest Points

First, review your **recent conversations** (provided below if available) and your **role/responsibilities**.
Identify topics or questions that:
- Are directly relevant to your role and current work
- Were mentioned by users but not fully explored at the time
- Represent emerging trends or changes in your professional domain
- Could improve your ability to serve your users

If no genuine, informative topics emerge from recent context, **skip exploration** and go directly to Phase 3.
Do NOT search for generic or obvious topics just to fill time. Quality over quantity.

## Phase 2: Targeted Exploration (Conditional)

Only if you identified genuine interest points in Phase 1:

1. Use the `linkup-search` skill (`POST https://api.linkup.so/v1/search`) to investigate (maximum 5 searches per heartbeat)
2. Keep searches **tightly scoped** to your role and recent work topics
3. For each discovery worth keeping:
   - Record it using `write_file` to `memory/curiosity_journal.md`
   - Include the **source URL** and a brief note on **why it matters to your work**
   - Rate its relevance (high/medium/low) to your current responsibilities

Format for curiosity_journal.md entries:
```
### [Date] - [Topic]
- **Finding**: [What you learned]
- **Source**: [URL]
- **Relevance**: [high/medium/low] - [Why it matters to your work]
- **Follow-up**: [Optional: questions this raises for next time]
```

## Phase 3: Agent Plaza

1. Call `plaza_get_new_posts` to check recent activity
2. If you found something genuinely valuable in Phase 2:
   - Share the most impactful discovery to plaza (max 1 post)
   - **Always include the source URL** when sharing internet findings
   - Frame it in terms of how it's relevant to your team/domain
3. Comment on relevant existing posts (max 2 comments)

## Phase 4: Wrap Up

- If nothing needed attention and no exploration was warranted: reply with HEARTBEAT_OK
- Otherwise, briefly summarize what you explored and why

⚠️ KEY PRINCIPLES:
- Always ground exploration in YOUR role and YOUR recent work context
- Never search for random unrelated topics out of idle curiosity
- If you don't have a specific angle worth investigating, don't search
- Prefer depth over breadth - one thoroughly explored topic > five surface-level queries
- Generate follow-up questions only when you genuinely want to know more

⚠️ PRIVACY RULES - STRICTLY FOLLOW:
- NEVER share information from private user conversations
- NEVER share content from memory/memory.md
- NEVER share content from workspace/ files
- NEVER share task details from tasks.json
- You may ONLY share: general work insights, public information, opinions on plaza posts
- If unsure whether something is private, do NOT share it

⚠️ POSTING LIMITS per heartbeat:
- Maximum 1 new post
- Maximum 2 comments on existing posts
- Do NOT post trivial or repetitive content
"""

PRIVATE_AGENT_HEARTBEAT_APPEND = """

⚠️ PRIVATE AGENT RULE - STRICTLY FOLLOW:
- You are a private agent. Do NOT browse Agent Plaza.
- Do NOT call plaza_get_new_posts, plaza_create_post, or plaza_add_comment.
- Do NOT share any findings, summaries, or opinions in Plaza.
- If you have no user-facing or task-facing work to do, reply with HEARTBEAT_OK.
"""


def _is_in_active_hours(active_hours: str, tz_name: str = "UTC") -> bool:
    """Check if current time is within the agent's active hours.

    Format: "HH:MM-HH:MM" (e.g., "09:00-18:00")
    Uses agent's configured timezone (defaults to UTC).
    """
    try:
        from zoneinfo import ZoneInfo

        start_str, end_str = active_hours.split("-")
        sh, sm = map(int, start_str.strip().split(":"))
        eh, em = map(int, end_str.strip().split(":"))
        try:
            tz = ZoneInfo(tz_name)
        except KeyError, Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        current_minutes = now.hour * 60 + now.minute
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        # Overnight range (e.g., "22:00-06:00")
        return current_minutes >= start_minutes or current_minutes < end_minutes
    except Exception:
        return True  # Default to active if parsing fails


async def _execute_heartbeat(agent_id: uuid.UUID):
    """Execute a single heartbeat for an agent.

    Uses three short DB transactions to avoid holding connections
    during long-running LLM calls:
      Phase 1: Read agent, model, context, notifications → commit
      Phase 2: LLM tool loop (no DB connection held)
      Phase 3: Write token usage → commit
    """
    _ = new_trace_id()
    _ = await _HEARTBEAT_SEMAPHORE.acquire()
    try:
        from app.dao.activity_log_dao import agent_activity_log_dao
        from app.dao.agent_dao import agent_dao
        from app.dao.llm_dao import llm_model_dao
        from app.dao.notification_dao import notification_dao
        from app.services.llm import get_model_api_key

        # ── Phase 1: Read all context from DB (short transactions via DAOs) ──
        agent_name = ""
        agent_role = ""
        agent_creator_id = None
        agent_is_private = False
        model_provider = ""
        model_api_key = ""
        model_model = ""
        model_base_url = None
        model_temperature = None
        model_max_output_tokens = None
        model_request_timeout = None
        heartbeat_instruction = DEFAULT_HEARTBEAT_INSTRUCTION

        agent = await agent_dao.get(agent_id)
        if not agent:
            return

        model_id = agent.primary_model_id or agent.fallback_model_id
        if not model_id:
            return

        model = await llm_model_dao.get(model_id)
        if not model:
            return

        # Cache values we need for Phase 2
        agent_name = agent.name
        agent_role = agent.role_description or ""
        agent_creator_id = agent.creator_id
        agent_is_private = (getattr(agent, "access_mode", None) or "company") != "company"
        model_provider = model.provider
        model_api_key = get_model_api_key(model)
        model_model = model.model
        model_base_url = model.base_url
        model_temperature = model.temperature
        model_max_output_tokens = getattr(model, "max_output_tokens", None)
        model_request_timeout = getattr(model, "request_timeout", None)

        # Read HEARTBEAT.md if it exists, otherwise use default
        storage = get_storage_backend()
        hb_key = agent_storage_key(agent_id, "HEARTBEAT.md")
        if await storage.exists(hb_key):
            try:
                custom = await storage.read_text(hb_key, encoding="utf-8", errors="replace")
                custom = custom.strip()
                if custom:
                    heartbeat_instruction = (
                        custom
                        + """

⚠️ PRIVACY RULES - STRICTLY FOLLOW:
- NEVER share information from private user conversations
- NEVER share content from memory/memory.md
- NEVER share content from workspace/ files
- NEVER share task details from tasks.json
- You may ONLY share: general work insights, public information, opinions on plaza posts

⚠️ POSTING LIMITS per heartbeat:
- Maximum 1 new post
- Maximum 2 comments on existing posts
- Do NOT post trivial or repetitive content
"""
                    )
            except Exception as error:
                logger.warning(f"Failed to read custom heartbeat instruction for {agent_id}: {error}")
        if agent_is_private:
            heartbeat_instruction += PRIVATE_AGENT_HEARTBEAT_APPEND

        from app.services.agent_context import build_agent_context

        static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, agent_role)

        recent_context = ""
        try:
            recent_activities = await agent_activity_log_dao.list_for_agent(
                agent_id,
                action_types=["chat_reply", "tool_call", "task_created", "task_updated"],
                limit=50,
            )
            if recent_activities:
                itms = []
                for act in reversed(list(recent_activities)):  # chronological order
                    ts = act.created_at.strftime("%m-%d %H:%M") if act.created_at else ""
                    itms.append(f"- [{ts}] {act.action_type}: {act.summary[:120]}")
                recent_context = (
                    "\\n\\n---\\n## Recent Activity Context\\nHere are your recent interactions and work to help you identify relevant topics:\\n\\n"
                    + "\\n".join(itms)
                )
        except Exception as e:
            logger.warning(f"Failed to fetch recent activity for heartbeat context: {e}")

        inbox_context = ""
        notif_lines = []
        try:
            unread = await notification_dao.list_unread_for_agent(agent_id, limit=10)
            if unread:
                notif_lines = ["\\n\\n---\\n## Inbox (new messages for you - please review and respond if appropriate)"]
                for n in unread:
                    sender = f"from {n.sender_name}" if n.sender_name else ""
                    notif_lines.append(f"- [{n.type}] {n.title} {sender}: {(n.body or '')[:150]}")
                await notification_dao.mark_read([n.id for n in unread])
        except Exception as e:
            logger.warning(f"Failed to drain agent notifications: {e}")

        inbox_context = "\\n".join(notif_lines)

        # ── Phase 2: LLM calls (no DB connection held) ──
        full_instruction = heartbeat_instruction + recent_context + inbox_context

        # Call LLM with tools using unified client
        from app.services.agent_tools import execute_tool, get_agent_tools_for_llm
        from app.services.llm import LLMError, LLMMessage, create_llm_client, get_max_tokens, get_model_api_key

        try:
            client = create_llm_client(
                provider=model_provider,
                api_key=model_api_key,
                model=model_model,
                base_url=model_base_url,
                timeout=float(model_request_timeout or 120.0),
            )
        except Exception as e:
            logger.error(f"Failed to create LLM client: {e}")
            return

        tool_catalog = await get_agent_tools_for_llm(agent_id)
        tools_for_llm: list[Any] = list(tool_catalog)

        reply = ""
        plaza_posts_made = 0  # hard limit: 1 new post per heartbeat
        plaza_comments_made = 0  # hard limit: 2 comments per heartbeat
        _hb_accumulated_usage = None
        _hb_unsaved_usage = None

        # Token tracking helpers
        from app.services.token_tracker import (
            TokenUsage,
            estimate_token_usage_from_chars,
            extract_token_usage,
            record_token_usage,
        )

        _hb_accumulated_usage = TokenUsage()
        _hb_unsaved_usage = TokenUsage()

        # Convert messages to LLMMessage format
        llm_messages = [
            LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt),
            LLMMessage(role="user", content=full_instruction),
        ]

        for round_i in range(20):  # More rounds for search + write + plaza
            # Check token usage limit mid-loop (every 3 rounds)
            if round_i > 0 and round_i % 3 == 0 and agent_id and _hb_unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _hb_unsaved_usage)
                _hb_unsaved_usage = TokenUsage()
                from app.services.llm.caller import _get_agent_config

                _, _token_limit_msg = await _get_agent_config(agent_id)
                if _token_limit_msg:
                    logger.warning(f"[Heartbeat] Token limit exceeded mid-loop: {_token_limit_msg}")
                    await client.close()
                    reply = _token_limit_msg
                    break

            try:
                response = await client.complete(
                    messages=llm_messages,
                    tools=tools_for_llm,
                    temperature=model_temperature,
                    max_tokens=get_max_tokens(model_provider, model_model, model_max_output_tokens),
                )
            except LLMError as e:
                logger.error(f"LLM error in heartbeat: {e}")
                reply = ""
                break
            except Exception as e:
                logger.error(f"LLM call error in heartbeat: {e}")
                reply = ""
                break

            # Track tokens for this round
            usage = extract_token_usage(response.usage)
            if not usage:
                round_chars = sum(len(m.content or "") for m in llm_messages) + len(response.content or "")
                usage = estimate_token_usage_from_chars(round_chars)
            _hb_accumulated_usage.add(usage)
            _hb_unsaved_usage.add(usage)

            if response.tool_calls:
                tool_call_parts = [_tool_call_parts(tool_call) for tool_call in response.tool_calls]
                # Add assistant message with tool calls
                llm_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=[
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": function,
                            }
                            for tool_call_id, _, function in tool_call_parts
                        ],
                        reasoning_content=response.reasoning_content,
                    )
                )

                finish_call = find_finish_call(response.tool_calls)
                if finish_call:
                    if finish_call.valid:
                        reply = finish_call.content
                        break
                    llm_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=finish_call.call_id,
                            content=finish_call.error or "`finish` was invalid.",
                        )
                    )
                    continue

                # Tools that require arguments - if LLM sends empty args, skip and ask to retry
                # (aligned with call_llm in websocket.py)
                tools_requiring_args = {
                    "write_file",
                    "read_file",
                    "delete_file",
                    "read_document",
                    "send_message_to_agent",
                    "send_feishu_message",
                    "send_email",
                }

                for tool_call_id, tool_name, function in tool_call_parts:
                    raw_args = function.get("arguments", "{}")
                    logger.info(
                        f"[Heartbeat] Raw arguments for {tool_name} (len={len(raw_args) if raw_args else 0}): "
                        + f"{_tool_argument_preview(raw_args, 300) if raw_args else 'None'}"
                    )
                    try:
                        args = parse_tool_arguments(raw_args)
                    except json.JSONDecodeError as je:
                        logger.warning(
                            f"[Heartbeat] JSON parse failed for {tool_name}: {je}. "
                            + f"Raw: {_tool_argument_preview(raw_args, 200)!r}"
                        )
                        args = {}

                    # Guard: if a tool that requires arguments received empty args,
                    # return an error to LLM instead of executing
                    if not args and tool_name in tools_requiring_args:
                        logger.warning(f"[Heartbeat] Empty arguments for {tool_name}, asking LLM to retry")
                        llm_messages.append(
                            LLMMessage(
                                role="tool",
                                tool_call_id=tool_call_id,
                                content=f"Error: {tool_name} was called with empty arguments. You must provide the required parameters. Please retry with the correct arguments.",
                            )
                        )
                        continue

                    # ── Hard rate limits for plaza actions ──
                    if tool_name == "plaza_create_post":
                        if plaza_posts_made >= 1:
                            tool_result = (
                                "[BLOCKED] You have already made 1 plaza post this heartbeat. Do not post again."
                            )
                        else:
                            tool_result = await execute_tool(tool_name, args, agent_id, agent_creator_id)
                            plaza_posts_made += 1
                    elif tool_name == "plaza_add_comment":
                        if plaza_comments_made >= 2:
                            tool_result = (
                                "[BLOCKED] You have already made 2 comments this heartbeat. Do not comment again."
                            )
                        else:
                            tool_result = await execute_tool(tool_name, args, agent_id, agent_creator_id)
                            plaza_comments_made += 1
                    else:
                        tool_result = await execute_tool(tool_name, args, agent_id, agent_creator_id)

                    llm_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=tool_call_id,
                            content=str(tool_result),
                        )
                    )
            else:
                if response.content:
                    llm_messages.append(LLMMessage(role="assistant", content=response.content))
                llm_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
                continue

        await client.close()

        # ── Phase 3: Write results back to DB ──
        if _hb_unsaved_usage and _hb_unsaved_usage.total_tokens > 0:
            await record_token_usage(agent_id, _hb_unsaved_usage)

        # Log activity if not empty
        is_ok = "HEARTBEAT_OK" in reply.upper().replace(" ", "_") if reply else False
        if not is_ok and reply:
            from app.services.activity_logger import log_activity

            await log_activity(
                agent_id,
                "heartbeat",
                f"Heartbeat: {reply[:80]}",
                detail={"reply": reply[:500]},
            )

        logger.info(f"💓 Heartbeat for {agent_name}: {'OK' if is_ok else reply[:60]}")

    except Exception as e:
        logger.exception(f"Heartbeat error for agent {agent_id}: {e}")
    finally:
        _HEARTBEAT_SEMAPHORE.release()


async def _heartbeat_tick():
    """One heartbeat tick: find agents due for heartbeat."""
    from app.dao.agent_dao import agent_dao
    from app.dao.tenant_dao import tenant_dao
    from app.services.audit_logger import write_audit_log
    from app.services.timezone_utils import get_agent_timezone_sync

    _ = new_trace_id()
    now = datetime.now(UTC)

    try:
        agents = list(await agent_dao.list_heartbeat_candidates())

        # Pre-load tenants for timezone resolution
        tenant_ids = {a.tenant_id for a in agents if a.tenant_id}
        tenants_by_id = {}
        if tenant_ids:
            tenants = await tenant_dao.get_by_ids(list(tenant_ids))
            tenants_by_id = {t.id: t for t in tenants}

        triggered = 0
        for agent in agents:
            # Skip / persist expired agents
            if agent.is_expired:
                continue
            if agent.expires_at and now >= agent.expires_at:
                _ = await agent_dao.mark_expired_stopped(agent)
                continue

            tenant_id = agent.tenant_id
            tenant = tenants_by_id.get(tenant_id) if tenant_id is not None else None
            tz_name = get_agent_timezone_sync(agent, tenant)

            if not _is_in_active_hours(agent.heartbeat_active_hours or "09:00-18:00", tz_name):
                continue

            interval = timedelta(minutes=agent.heartbeat_interval_minutes or 240)
            if agent.last_heartbeat_at and (now - agent.last_heartbeat_at) < interval:
                continue

            claimed = await agent_dao.claim_heartbeat(agent.id, now=now, interval=interval)
            if not claimed:
                continue

            logger.info(f"💓 Triggering heartbeat for {agent.name}")
            try:
                await write_audit_log("heartbeat_fire", {"agent_name": agent.name}, agent_id=agent.id)
            except Exception as e:
                logger.warning(f"Failed to write heartbeat_fire audit log for {agent.name}: {e}")
            task = asyncio.create_task(_execute_heartbeat(agent.id))
            _HEARTBEAT_TASKS.add(task)
            task.add_done_callback(_observe_heartbeat_task)
            triggered += 1

        if triggered:
            try:
                await write_audit_log("heartbeat_tick", {"eligible_agents": len(agents), "triggered": triggered})
            except Exception as e:
                logger.warning(f"Failed to write heartbeat_tick audit log: {e}")

    except Exception as e:
        logger.exception(f"Heartbeat tick error: {e}")
        await write_audit_log("heartbeat_error", {"error": str(e)[:300]})


async def start_heartbeat():
    """Start the background heartbeat loop. Call from FastAPI startup."""
    logger.info("💓 Agent heartbeat service started (60s tick)")
    while True:
        await _heartbeat_tick()
        await asyncio.sleep(60)


async def _notify_oneshot_error(
    triggered_by_user_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    agent_name: str,
    error_msg: str,
) -> None:
    """Create a platform notification for the admin who triggered a failed oneshot task."""
    if not triggered_by_user_id:
        return
    try:
        from app.dao.notification_dao import notification_dao

        _ = await notification_dao.create(
            obj_in={
                "user_id": triggered_by_user_id,
                "type": "system",
                "title": f"{agent_name} task failed",
                "body": error_msg[:500],
                "link": f"/agents/{agent_id}#chat",
                "ref_id": agent_id,
                "sender_name": agent_name,
                "is_read": False,
            }
        )
        logger.info(f"[Oneshot] Notified user {triggered_by_user_id} about {agent_name} failure")
    except Exception as e:
        logger.warning(f"[Oneshot] Failed to create error notification: {e}")


async def run_agent_oneshot(
    agent_id: uuid.UUID,
    prompt: str,
    triggered_by_user_id: uuid.UUID | None = None,
    max_rounds: int = 40,
) -> str:
    """Run an agent with a specific one-shot task prompt.

    Reuses the same LLM + tools infrastructure as the heartbeat, but:
    - Accepts an arbitrary task prompt instead of the HEARTBEAT.md instruction
    - Does NOT update last_heartbeat_at
    - Does NOT check active hours
    - Configurable max_rounds to handle multi-member outreach tasks
    - Sends a platform notification to the triggering user on failure

    Returns the final reply string (for logging purposes).
    """
    _ = new_trace_id()
    try:
        from app.dao.agent_dao import agent_dao
        from app.dao.llm_dao import llm_model_dao
        from app.services.llm import get_model_api_key

        # ── Phase 1: Read agent + model config ─────────────────────────────────
        agent_name = ""
        agent_role = ""
        agent_creator_id = None
        model_provider = ""
        model_api_key = ""
        model_model = ""
        model_base_url = None
        model_temperature = None
        model_max_output_tokens = None
        model_request_timeout = None

        agent = await agent_dao.get(agent_id)
        if not agent:
            logger.warning(f"[Oneshot] Agent {agent_id} not found - aborting")
            return ""

        model_id = agent.primary_model_id or agent.fallback_model_id
        if not model_id:
            msg = "Agent has no LLM model configured. Please assign a model in Agent Settings."
            logger.warning(f"[Oneshot] Agent {agent_id} has no model configured - aborting")
            await _notify_oneshot_error(triggered_by_user_id, agent_id, agent_name or str(agent_id), msg)
            return ""

        model = await llm_model_dao.get(model_id)
        if not model:
            msg = f"The configured LLM model ({model_id}) was not found. Please check Agent Settings."
            logger.warning(f"[Oneshot] Model {model_id} not found - aborting")
            await _notify_oneshot_error(triggered_by_user_id, agent_id, agent_name or str(agent_id), msg)
            return ""

        agent_name = agent.name
        agent_role = agent.role_description or ""
        agent_creator_id = agent.creator_id
        model_provider = model.provider
        model_api_key = get_model_api_key(model)
        model_model = model.model
        model_base_url = model.base_url
        model_temperature = model.temperature
        model_max_output_tokens = getattr(model, "max_output_tokens", None)
        model_request_timeout = getattr(model, "request_timeout", None)

        from app.services.agent_context import build_agent_context

        static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, agent_role)

        # ── Phase 2: LLM tool-call loop (no DB connection held) ────────────────
        from app.services.agent_tools import execute_tool, get_agent_tools_for_llm
        from app.services.llm import (
            LLMError,
            LLMMessage,
            create_llm_client,
            get_max_tokens,
        )
        from app.services.token_tracker import (
            TokenUsage,
            estimate_token_usage_from_chars,
            extract_token_usage,
            record_token_usage,
        )

        try:
            client = create_llm_client(
                provider=model_provider,
                api_key=model_api_key,
                model=model_model,
                base_url=model_base_url,
                timeout=float(model_request_timeout or 120.0),
            )
        except Exception as e:
            msg = f"Failed to initialise the LLM client: {e}"
            logger.error(f"[Oneshot] Failed to create LLM client for {agent_name}: {e}")
            await _notify_oneshot_error(triggered_by_user_id, agent_id, agent_name, msg)
            return ""

        tool_catalog = await get_agent_tools_for_llm(agent_id)
        tools_for_llm: list[Any] = list(tool_catalog)
        llm_messages = [
            LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt),
            LLMMessage(role="user", content=prompt),
        ]

        reply = ""
        accumulated_usage = TokenUsage()
        unsaved_usage = TokenUsage()

        completed_rounds = 0
        for round_i in range(max_rounds):
            completed_rounds = round_i + 1
            # Check token usage limit mid-loop (every 3 rounds)
            if round_i > 0 and round_i % 3 == 0 and agent_id and unsaved_usage.total_tokens > 0:
                try:
                    await record_token_usage(agent_id, unsaved_usage)
                except Exception as e:
                    logger.warning(f"[Oneshot] Failed to record token usage mid-loop: {e}")
                unsaved_usage = TokenUsage()
                from app.services.llm.caller import _get_agent_config

                _, _token_limit_msg = await _get_agent_config(agent_id)
                if _token_limit_msg:
                    logger.warning(f"[Oneshot] Token limit exceeded mid-loop: {_token_limit_msg}")
                    await client.close()
                    reply = _token_limit_msg
                    break

            try:
                response = await client.complete(
                    messages=llm_messages,
                    tools=tools_for_llm,
                    temperature=model_temperature,
                    max_tokens=get_max_tokens(model_provider, model_model, model_max_output_tokens),
                )
            except LLMError as e:
                logger.error(f"[Oneshot] LLM error (round {round_i}): {e}")
                await _notify_oneshot_error(
                    triggered_by_user_id,
                    agent_id,
                    agent_name,
                    f"LLM call failed (round {round_i}): {e}",
                )
                break
            except Exception as e:
                logger.error(f"[Oneshot] Unexpected LLM error (round {round_i}): {e}")
                await _notify_oneshot_error(
                    triggered_by_user_id,
                    agent_id,
                    agent_name,
                    f"Unexpected error during LLM call (round {round_i}): {e}",
                )
                break

            # Track token usage
            usage = extract_token_usage(response.usage)
            if not usage:
                round_chars = sum(len(m.content or "") for m in llm_messages) + len(response.content or "")
                usage = estimate_token_usage_from_chars(round_chars)
            accumulated_usage.add(usage)
            unsaved_usage.add(usage)

            if response.tool_calls:
                tool_call_parts = [_tool_call_parts(tool_call) for tool_call in response.tool_calls]
                llm_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=[
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": function,
                            }
                            for tool_call_id, _, function in tool_call_parts
                        ],
                        reasoning_content=response.reasoning_content,
                    )
                )

                finish_call = find_finish_call(response.tool_calls)
                if finish_call:
                    if finish_call.valid:
                        reply = finish_call.content
                        break
                    llm_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=finish_call.call_id,
                            content=finish_call.error or "`finish` was invalid.",
                        )
                    )
                    continue

                for tool_call_id, tool_name, function in tool_call_parts:
                    raw_args = function.get("arguments", "{}")
                    try:
                        args = parse_tool_arguments(raw_args)
                    except json.JSONDecodeError:
                        args = {}

                    logger.info(f"[Oneshot:{agent_name}] Tool call: {tool_name}({list(args.keys())})")
                    tool_result = await execute_tool(tool_name, args, agent_id, agent_creator_id)

                    llm_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=tool_call_id,
                            content=str(tool_result),
                        )
                    )
            else:
                if response.content:
                    llm_messages.append(LLMMessage(role="assistant", content=response.content))
                llm_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
                continue

        await client.close()

        # ── Phase 3: Record token usage (best-effort) ───────────────────────────
        if unsaved_usage.total_tokens > 0:
            try:
                await record_token_usage(agent_id, unsaved_usage)
            except Exception as e:
                logger.warning(f"[Oneshot] Failed to record token usage: {e}")

        # Log activity
        if reply:
            try:
                from app.services.activity_logger import log_activity

                await log_activity(
                    agent_id,
                    "oneshot_task",
                    f"Oneshot task completed: {reply[:80]}",
                    detail={"reply": reply[:500], "triggered_by": str(triggered_by_user_id)},
                )
            except Exception as error:
                logger.warning(f"Failed to log oneshot activity for {agent_id}: {error}")

        # ── Phase 4: Clear any previous error notifications ──────────
        if triggered_by_user_id:
            try:
                from app.dao.notification_dao import notification_dao

                await notification_dao.delete_system_task_failed(
                    user_id=triggered_by_user_id,
                    agent_id=agent_id,
                )
            except Exception as e:
                logger.warning(f"[Oneshot] Failed to clear error notifications: {e}")

        logger.info(
            f"[Oneshot] {agent_name} completed ({completed_rounds} rounds, {accumulated_usage.total_tokens} tokens)"
        )
        return reply

    except Exception as e:
        logger.exception(f"[Oneshot] Unexpected error for agent {agent_id}: {e}")
        return ""
