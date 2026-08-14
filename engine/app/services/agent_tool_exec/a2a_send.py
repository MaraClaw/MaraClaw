from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import logger
from app.core.permissions import evaluate_agent_relationship_status
from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_dao import agent_dao
from app.dao.audit_log_dao import audit_log_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.participant_dao import participant_dao
from app.services import agent_tools
from app.services.audit_logger import write_audit_log

from .a2a_context import _resolve_target_agent
from .registry import ToolArguments


async def _send_file_to_agent(from_agent_id: uuid.UUID, args: ToolArguments) -> str:
    """Send a workspace file to another digital employee (agent)."""
    agent_name = _string_argument(args, "agent_name")
    rel_path = _string_argument(args, "file_path")
    delivery_note = _string_argument(args, "message")

    if not agent_name or not rel_path:
        return "❌ Please provide both agent_name and file_path"

    storage = agent_tools.get_storage_backend()
    source_key = agent_tools.normalize_storage_key(f"{from_agent_id}/{rel_path}")
    if not await storage.is_file(source_key):
        return f"❌ Source file not found: {rel_path}"
    source_entry = await storage.stat(source_key)

    max_file_size = 50 * 1024 * 1024
    file_size = source_entry.size
    if file_size > max_file_size:
        size_mb = file_size / (1024 * 1024)
        return f"❌ File too large ({size_mb:.1f} MB). Maximum allowed is 50 MB."
    source_bytes = await storage.read_bytes(source_key)
    source_name = Path(rel_path).name

    try:
        from app.services.activity_logger import log_activity

        source_agent = await agent_dao.get(from_agent_id)
        source_agent_name = source_agent.name if source_agent else "Unknown agent"
        source_tenant_id = source_agent.tenant_id if source_agent else None
        source_creator_id = source_agent.creator_id if source_agent else from_agent_id

        target_agent = await _resolve_target_agent(
            from_agent_id=from_agent_id,
            agent_name=agent_name,
            source_tenant_id=source_tenant_id,
        )

        if not target_agent:
            rels = await agent_agent_relationship_dao.list_for_agent_with_targets(from_agent_id)
            rel_names = [r.target_agent.name for r in rels if r.target_agent]
            return (
                f"❌ No agent found matching '{agent_name}'. Your connected colleagues: "
                + f"{', '.join(rel_names) if rel_names else 'none - ask your administrator to set up relationships'}"
            )

        if target_agent.is_expired or (target_agent.expires_at and datetime.now(UTC) >= target_agent.expires_at):
            return (
                f"⚠️ {target_agent.name} is currently unavailable - their service period has ended. "
                + "Please contact the platform administrator."
            )

        rels = await agent_agent_relationship_dao.list_for_agent(from_agent_id)
        rel = next((r for r in rels if r.target_agent_id == target_agent.id), None)
        if not rel:
            return (
                f"❌ You do not have a relationship with {target_agent.name}. Only agents in your relationship list "
                + "can receive files. Ask your administrator to add a relationship if needed."
            )
        status_info = await evaluate_agent_relationship_status(None, rel)
        if status_info["access_status"] != "active":
            return (
                f"❌ Relationship to {target_agent.name} is not active "
                + f"({status_info['access_status_reason'] or 'restricted'}). "
                + "Ask a manager of both agents to review Relationships."
            )

        target_name = target_agent.name
        target_id = target_agent.id

        ts = datetime.now(UTC)
        stamp = ts.strftime("%Y%m%d_%H%M%S_%f")
        delivered_name = source_name
        target_rel_path = f"workspace/inbox/files/{delivered_name}"
        target_key = agent_tools.normalize_storage_key(f"{target_id}/{target_rel_path}")
        while await storage.exists(target_key):
            delivered_name = f"{stamp}_{source_name}"
            target_rel_path = f"workspace/inbox/files/{delivered_name}"
            target_key = agent_tools.normalize_storage_key(f"{target_id}/{target_rel_path}")

        await storage.write_bytes(target_key, source_bytes)

        sender_short = str(from_agent_id)[:8]
        note_rel_path = f"workspace/inbox/{stamp}_{sender_short}_file_delivery.md"
        note_key = agent_tools.normalize_storage_key(f"{target_id}/{note_rel_path}")
        note_lines = [
            f"# File delivery from {source_agent_name}",
            "",
            f"- Time (UTC): {ts.isoformat()}",
            f"- Sender: {source_agent_name}",
            f"- Source path: {rel_path}",
            f"- Delivered file: {target_rel_path}",
            "",
        ]
        if delivery_note:
            note_lines.append("## Note")
            note_lines.append(delivery_note)
            note_lines.append("")
        note_lines.append("## Action")
        note_lines.append(f'- Read the file via `read_file(path="{target_rel_path}")`')
        await storage.write_text(note_key, "\n".join(note_lines), encoding="utf-8")

        # Prefer write_audit_log; fall back to DAO create for dual-stack compatibility.
        try:
            await write_audit_log(
                action="collaboration:file_send",
                details={
                    "to_agent": str(target_id),
                    "to_agent_name": target_name,
                    "source_file": rel_path,
                    "delivered_file": target_rel_path,
                },
                agent_id=from_agent_id,
            )
            await write_audit_log(
                action="collaboration:file_receive",
                details={
                    "from_agent": str(from_agent_id),
                    "from_agent_name": source_agent_name,
                    "source_file": rel_path,
                    "delivered_file": target_rel_path,
                },
                agent_id=target_id,
            )
        except Exception:
            _ = await audit_log_dao.create(
                obj_in={
                    "agent_id": from_agent_id,
                    "action": "collaboration:file_send",
                    "details": {
                        "to_agent": str(target_id),
                        "to_agent_name": target_name,
                        "source_file": rel_path,
                        "delivered_file": target_rel_path,
                    },
                }
            )
            _ = await audit_log_dao.create(
                obj_in={
                    "agent_id": target_id,
                    "action": "collaboration:file_receive",
                    "details": {
                        "from_agent": str(from_agent_id),
                        "from_agent_name": source_agent_name,
                        "source_file": rel_path,
                        "delivered_file": target_rel_path,
                    },
                }
            )

        await log_activity(
            from_agent_id,
            "agent_file_sent",
            f"Sent file to {target_name}",
            detail={"target_agent": target_name, "source_file": rel_path, "delivered_file": target_rel_path},
        )
        await log_activity(
            target_id,
            "agent_file_received",
            f"Received file from {source_agent_name}",
            detail={"source_agent": source_agent_name, "source_file": rel_path, "delivered_file": target_rel_path},
        )

        logger.info(
            "[A2A-File] Injecting file delivery message: from=%s to=%s file=%s",
            source_agent_name,
            target_name,
            delivered_name,
        )
        try:
            session_agent_id = min(from_agent_id, target_id, key=str)
            session_peer_id = max(from_agent_id, target_id, key=str)
            chat_session = await chat_session_dao.get_agent_peer_session(
                session_agent_id=session_agent_id,
                peer_agent_id=session_peer_id,
            )
            if not chat_session:
                src_participant = await participant_dao.get_by_type_ref("agent", from_agent_id)
                chat_session = await chat_session_dao.create(
                    obj_in={
                        "agent_id": session_agent_id,
                        "user_id": source_creator_id,
                        "title": f"{source_agent_name} ↔ {target_name}",
                        "source_channel": "agent",
                        "participant_id": src_participant.id if src_participant else None,
                        "peer_agent_id": session_peer_id,
                    }
                )

            file_msg_content = (
                f"[File delivery from {source_agent_name}]\n"
                + f"{source_agent_name} sent you a file: {delivered_name}\n"
                + f"File path: {target_rel_path}\n"
                + f'Use read_file(path="{target_rel_path}") to inspect it.'
            )
            if delivery_note:
                file_msg_content += f"\nNote: {delivery_note}"

            src_part2 = await participant_dao.get_by_type_ref("agent", from_agent_id)
            _ = await chat_message_dao.insert_message(
                agent_id=session_agent_id,
                user_id=source_creator_id,
                role="user",
                content=file_msg_content,
                conversation_id=str(chat_session.id),
                participant_id=src_part2.id if src_part2 else None,
            )
            _ = await chat_session_dao.update(db_obj=chat_session, obj_in={"last_message_at": ts})
            logger.info(
                "[A2A-File] Injected file delivery message into session %s for %s",
                chat_session.id,
                target_name,
            )
        except Exception as error:
            logger.error(f"[A2A-File] FAILED to inject file delivery message: {error}")

        return f"✅ File sent to {target_name}.\n- Delivered to: {target_rel_path}\n- Inbox note: {note_rel_path}"
    except Exception as error:
        return f"❌ Agent file send error: {str(error)[:200]}"


async def _send_message_to_agent(
    from_agent_id: uuid.UUID,
    args: ToolArguments,
    user_id: uuid.UUID | None = None,
    origin_session_id: str | None = None,
) -> str:
    """Send a message to another digital employee."""
    ctx_or_err = await agent_tools._build_a2a_context(from_agent_id, args, user_id, origin_session_id)
    if isinstance(ctx_or_err, str):
        return ctx_or_err
    ctx = ctx_or_err

    if ctx.target_agent.agent_type == "openclaw":
        return await agent_tools._a2a_handle_openclaw(ctx)
    if ctx.msg_type == "notify":
        return await agent_tools._a2a_handle_notify(ctx)
    if ctx.msg_type == "task_delegate":
        return await agent_tools._a2a_handle_task_delegate(ctx)
    return await agent_tools._a2a_handle_consult(ctx)


def _string_argument(arguments: ToolArguments, name: str) -> str:
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""
