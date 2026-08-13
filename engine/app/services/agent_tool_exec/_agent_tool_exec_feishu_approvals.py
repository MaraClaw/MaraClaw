from __future__ import annotations

import importlib
import json
import uuid

from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments


def _feishu_service():
    return importlib.import_module("app.services.feishu_service").feishu_service


async def _feishu_approval_create(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    approval_code_value = arguments.get("approval_code", "")
    user_id_value = arguments.get("user_id", "")
    form_data_value = arguments.get("form_data", "")
    approval_code = approval_code_value.strip() if isinstance(approval_code_value, str) else ""
    user_id = user_id_value.strip() if isinstance(user_id_value, str) else ""
    form_data = form_data_value.strip() if isinstance(form_data_value, str) else ""
    if not approval_code or not user_id or not form_data:
        return "❌ form_data, user_id and approval_code are required."

    try:
        response = await _feishu_service().create_approval_instance(
            app_id, app_secret, approval_code, user_id, form_data
        )
        error = agent_tools._check_feishu_err(response)
        if error:
            return error
        instance_code = response.get("data", {}).get("instance_code", "")
        return f"✅ 审批发起成功！\n审批实例 ID: `{instance_code}`"
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _feishu_approval_query(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    approval_code_value = arguments.get("approval_code", "")
    status_value = arguments.get("status")
    approval_code = approval_code_value.strip() if isinstance(approval_code_value, str) else ""
    status = status_value if isinstance(status_value, str) else None
    if not approval_code:
        return "❌ approval_code is required."

    try:
        response = await _feishu_service().query_approval_instances(app_id, app_secret, approval_code, status)
        error = agent_tools._check_feishu_err(response)
        if error:
            return error
        instance_codes = response.get("data", {}).get("instance_code_list", [])
        return f"✅ 查询完成。共发现 {len(instance_codes)} 个符合条件的审批实例。\n实例列表: {instance_codes}"
    except Exception as error:
        return f"Failed: {str(error)[:300]}"


async def _feishu_approval_get(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    app_id, app_secret = await agent_tools._get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    instance_id_value = arguments.get("instance_id", "")
    instance_id = instance_id_value.strip() if isinstance(instance_id_value, str) else ""
    if not instance_id:
        return "❌ instance_id is required."

    try:
        response = await _feishu_service().get_approval_instance(app_id, app_secret, instance_id)
        error = agent_tools._check_feishu_err(response)
        if error:
            return error
        data = response.get("data", {})
        return f"✅ 审批实例查询结果:\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"
    except Exception as error:
        return f"Failed: {str(error)[:300]}"
