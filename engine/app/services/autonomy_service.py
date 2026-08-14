"""Autonomy boundary enforcement service.

Implements the three-level autonomy system:
  L1 - Auto-execute, notify creator
  L2 - Notify creator, auto-execute
  L3 - Require explicit approval before execution
"""

from __future__ import annotations

import json
import uuid

from app.core.json_types import JsonObject, is_str_dict, json_as_str_or, mapping_from_row, object_from_literal
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.approval_dao import approval_request_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.user_dao import user_dao
from app.records.agent import AgentRecord
from app.records.audit import ApprovalRequestRecord
from app.records.user import UserRecord
from app.services.audit_logger import write_audit_log
from app.services.feishu_service import feishu_service


class AutonomyService:
    """Enforce autonomy boundaries for agent operations."""

    async def check_and_enforce(
        self,
        db: object | None = None,
        agent: AgentRecord | None = None,
        action_type: str = "",
        details: JsonObject | None = None,
    ) -> JsonObject:
        """Check if an action is allowed under the agent's autonomy policy.

        ``db`` is accepted for dual-stack call-site compatibility and ignored;
        persistence goes through DAOs / connection_ctx.

        Returns:
            {
                "allowed": True/False,
                "level": "L1"/"L2"/"L3",
                "approval_id": uuid (if L3),
                "message": str,
            }
        """
        if agent is None:
            return {"allowed": False, "level": "unknown", "message": "Agent required"}
        details = details or {}
        policy = mapping_from_row(agent.autonomy_policy)
        level = json_as_str_or(dict[str, object](policy).get(action_type), "L2") or "L2"

        await write_audit_log(
            action=f"autonomy_check:{action_type}",
            details={"level": level, **details},
            agent_id=agent.id,
        )

        if level == "L1":
            logger.info(f"L1: Auto-executing {action_type} for agent {agent.name}")
            return {
                "allowed": True,
                "level": "L1",
                "message": "Auto-executed",
            }

        if level == "L2":
            logger.info(f"L2: Executing {action_type} for agent {agent.name} with notification")
            await self._notify_creator(agent, action_type, details)
            return {
                "allowed": True,
                "level": "L2",
                "message": "Executed and creator notified",
            }

        if level == "L3":
            approval = await approval_request_dao.create_pending(
                agent_id=agent.id,
                action_type=action_type,
                details=details,
            )

            logger.info(f"L3: Approval required for {action_type} by agent {agent.name}")
            await self._request_approval(agent, approval)

            return {
                "allowed": False,
                "level": "L3",
                "approval_id": str(approval.id),
                "message": "Approval requested from creator",
            }

        return {"allowed": False, "level": "unknown", "message": "Unknown autonomy level"}

    async def resolve_approval(
        self,
        db: object | None = None,
        approval_id: uuid.UUID | None = None,
        user: UserRecord | None = None,
        action: str = "",
    ) -> ApprovalRequestRecord:
        """Approve or reject a pending approval request.

        ``db`` is accepted for dual-stack call-site compatibility and ignored.
        """
        if approval_id is None or user is None:
            raise ValueError("Approval not found")

        approval = await approval_request_dao.get(approval_id)
        if not approval:
            raise ValueError("Approval not found")

        if approval.status != "pending":
            raise ValueError("Approval already resolved")

        agent = await agent_dao.get(approval.agent_id)
        if agent and agent.creator_id != user.id and user.role != "platform_admin":
            raise ValueError("Only the agent creator or platform admin can resolve approvals")

        status = "approved" if action == "approve" else "rejected"
        approval = await approval_request_dao.resolve(
            approval,
            status=status,
            resolved_by=user.id,
        )

        await write_audit_log(
            action=f"approval_{approval.status}",
            details={"approval_id": str(approval.id), "action_type": approval.action_type},
            agent_id=approval.agent_id,
            user_id=user.id,
        )

        execution_result = None
        if approval.status == "approved" and approval.details:
            execution_result = await self._execute_approved_action(
                approval.agent_id, approval.action_type, approval.details
            )
            logger.info(f"Post-approval execution for {approval.action_type}: {execution_result}")

        if agent:
            from app.services.notification_service import send_notification

            status_label = "approved" if approval.status == "approved" else "rejected"
            body_text = json.dumps(approval.details, ensure_ascii=False)[:200]
            if execution_result:
                body_text = f"Result: {execution_result}"
            _ = await send_notification(
                None,
                user_id=agent.creator_id,
                type="approval_resolved",
                title=f"[{agent.name}] {approval.action_type} - {status_label}",
                body=body_text,
                link=f"/agents/{agent.id}#approvals",
                ref_id=approval.id,
            )

            requested_by = approval.details.get("requested_by") if approval.details else None
            if isinstance(requested_by, str) and requested_by:
                try:
                    requester_id = uuid.UUID(requested_by)
                    if requester_id != agent.creator_id:
                        _ = await send_notification(
                            None,
                            user_id=requester_id,
                            type="approval_resolved",
                            title=f"[{agent.name}] {approval.action_type} - {status_label}",
                            body=body_text,
                            link=f"/agents/{agent.id}#activityLog",
                            ref_id=approval.id,
                        )
                except ValueError:
                    pass

        return approval

    async def _execute_approved_action(self, agent_id: uuid.UUID, action_type: str, details: JsonObject) -> str | None:
        """Execute the tool action that was approved.

        Reads the tool name and arguments from the approval details,
        then directly calls the tool executor (bypassing autonomy check).
        """
        _ = action_type
        tool_name = details.get("tool")
        args_raw = details.get("args", "{}")
        if not isinstance(tool_name, str) or not tool_name:
            return None

        try:
            if isinstance(args_raw, str):
                try:
                    arguments = object_from_literal(args_raw)
                except ValueError, SyntaxError, json.JSONDecodeError:
                    return "Execution failed: approved action arguments must be a JSON object"
            else:
                arguments = args_raw

            if not is_str_dict(arguments):
                return "Execution failed: approved action arguments must be a JSON object"

            from app.services.agent_tool_exec.dispatcher import _execute_tool_direct

            return await _execute_tool_direct(tool_name, arguments, agent_id)
        except Exception as e:
            logger.error(f"Failed to execute approved action {tool_name}: {e}")
            return f"Execution failed: {e}"

    async def _notify_creator(self, agent: AgentRecord, action_type: str, details: JsonObject) -> None:
        """Send L2 notification to agent creator via Feishu + web."""
        from app.services.notification_service import send_notification

        _ = await send_notification(
            None,
            user_id=agent.creator_id,
            type="autonomy_l2",
            title=f"[{agent.name}] executed: {action_type}",
            body=json.dumps(details, ensure_ascii=False)[:200],
            link=f"/agents/{agent.id}#activityLog",
        )

        channel = await channel_config_dao.get_first_for_agent(agent.id)
        if not channel or not channel.app_id or not channel.app_secret:
            return

        creator = await user_dao.get(agent.creator_id)
        if not creator:
            return

        provider = await identity_provider_dao.get_by_type_and_tenant("feishu", creator.tenant_id)
        if not provider:
            return

        members = await org_member_dao.list_by_user_and_provider(creator.id, provider.id)
        member = members[0] if members else None
        if not member:
            return

        external_id = member.external_id
        open_id = member.open_id
        if isinstance(external_id, str) and external_id:
            receive_id = external_id
            id_type = "user_id"
        elif isinstance(open_id, str) and open_id:
            receive_id = open_id
            id_type = "open_id"
        else:
            return
        _ = await feishu_service.send_message(
            channel.app_id,
            channel.app_secret,
            receive_id,
            "text",
            json.dumps({"text": f"[{agent.name}] executed: {action_type}"}),
            receive_id_type=id_type,
        )

    async def _request_approval(self, agent: AgentRecord, approval: ApprovalRequestRecord) -> None:
        """Send L3 approval request to creator via Feishu card + web notification."""
        from app.services.notification_service import send_notification

        _ = await send_notification(
            None,
            user_id=agent.creator_id,
            type="approval_pending",
            title=f"[{agent.name}] requests approval: {approval.action_type}",
            body=json.dumps(approval.details, ensure_ascii=False)[:200],
            link=f"/agents/{agent.id}#approvals",
            ref_id=approval.id,
        )

        channel = await channel_config_dao.get_first_for_agent(agent.id)
        if not channel or not channel.app_id or not channel.app_secret:
            return

        creator = await user_dao.get(agent.creator_id)
        if not creator:
            return

        provider = await identity_provider_dao.get_by_type_and_tenant("feishu", creator.tenant_id)
        if not provider:
            return

        members = await org_member_dao.list_by_user_and_provider(creator.id, provider.id)
        member = members[0] if members else None
        if not member:
            return

        external_id = member.external_id
        open_id = member.open_id
        if isinstance(external_id, str) and external_id:
            receive_id = external_id
        elif isinstance(open_id, str) and open_id:
            receive_id = open_id
        else:
            return
        _ = await feishu_service.send_approval_card(
            channel.app_id,
            channel.app_secret,
            receive_id,
            agent.name,
            approval.action_type,
            json.dumps(approval.details, ensure_ascii=False),
            str(approval.id),
        )


autonomy_service = AutonomyService()
