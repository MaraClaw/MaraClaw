from __future__ import annotations

import importlib
import re
import uuid

from app.config import get_settings
from app.dao.channel_config_dao import channel_config_dao

from .registry import ToolArgumentValue


def _httpx_module():
    return importlib.import_module("httpx")


async def _get_feishu_credentials(agent_id: uuid.UUID) -> tuple[str, str]:
    settings = get_settings()
    app_id = settings.FEISHU_APP_ID
    app_secret = settings.FEISHU_APP_SECRET

    try:
        config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="feishu")
        if config and config.app_id and config.app_secret:
            app_id = config.app_id
            app_secret = config.app_secret
    except Exception:
        return app_id, app_secret

    return app_id, app_secret


async def _get_feishu_tenant_doc_url(tenant_token: str, doc_token: str, doc_type: str = "docx") -> str:
    try:
        async with _httpx_module().AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://open.feishu.cn/open-apis/tenant/v2/tenant/query",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        data = resp.json()
        if data.get("code") == 0:
            domain = data.get("data", {}).get("tenant", {}).get("domain", "")
            if domain:
                return f"https://{domain}/{doc_type}/{doc_token}"
    except Exception:
        return f"https://feishu.cn/{doc_type}/{doc_token}"
    return f"https://feishu.cn/{doc_type}/{doc_token}"


async def _get_feishu_bitable_url(tenant_token: str, app_token: str, table_id: str = "") -> str:
    try:
        async with _httpx_module().AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://open.feishu.cn/open-apis/tenant/v2/tenant/query",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        data = resp.json()
        if data.get("code") == 0:
            domain = data.get("data", {}).get("tenant", {}).get("domain", "")
            if domain:
                base_url = f"https://{domain}/base/{app_token}"
                if table_id:
                    base_url += f"?table={table_id}"
                return base_url
    except Exception:
        base_url = f"https://feishu.cn/base/{app_token}"
        if table_id:
            base_url += f"?table={table_id}"
        return base_url
    base_url = f"https://feishu.cn/base/{app_token}"
    if table_id:
        base_url += f"?table={table_id}"
    return base_url


def _parse_feishu_url(url: str) -> dict[str, str]:
    result: dict[str, str] = {}

    base_match = re.search(r"/base/([a-zA-Z0-9_]+)", url)
    if base_match:
        result["app_token"] = base_match.group(1)

    table_match = re.search(r"table=([a-zA-Z0-9_]+)", url)
    if table_match:
        result["table_id"] = table_match.group(1)

    if "table_id" not in result:
        tbl_match = re.search(r"/(tbl[a-zA-Z0-9_]+)", url)
        if tbl_match:
            result["table_id"] = tbl_match.group(1)

    view_match = re.search(r"view=([a-zA-Z0-9_]+)", url)
    if view_match:
        result["view_id"] = view_match.group(1)

    docx_match = re.search(r"/docx/([a-zA-Z0-9_]+)", url)
    if docx_match:
        result["document_token"] = docx_match.group(1)

    wiki_match = re.search(r"/wiki/([a-zA-Z0-9_]+)", url)
    if wiki_match:
        result["wiki_token"] = wiki_match.group(1)

    return result


def _check_feishu_err(resp: dict[str, ToolArgumentValue]) -> str | None:
    code = resp.get("code")
    if code != 0:
        msg = str(resp.get("msg", ""))
        msg_lower = msg.lower()
        perm_codes = {99991663, 10006, 99991661, 99991668, 91403, 1063001, 1063004}
        perm_keywords = ("permission", "forbidden", "no access", "access denied", "403")
        is_perm_error = code in perm_codes or any(keyword in msg_lower for keyword in perm_keywords)
        if is_perm_error:
            return (
                f"Failed: Permission denied (code: {code}, msg: {msg}). "
                + "The bot app does not have access to this document/Bitable. "
                + "CRITICAL INSTRUCTION for the Agent: You MUST explicitly output ALL of the following "
                + "step-by-step instructions to the user in your reply. Do NOT omit or summarize any step:\n\n"
                + "---\n"
                + "**The bot needs to be added to this document/table as an authorized app. "
                + "Please follow these steps:**\n\n"
                + "1. Open the target document or Bitable in the Feishu client (web or desktop).\n"
                + "2. Click the **「...」** menu button in the top-right corner of the page.\n"
                + "3. In the dropdown menu, hover over **「更多」** (More) at the bottom.\n"
                + "4. In the sub-menu that appears, click **「添加文档应用」** (Add Document App).\n"
                + "5. In the search box, type the name of your Feishu bot app (the one bound to this Agent's channel), then click to add it.\n"
                + "6. After adding, retry the same operation.\n\n"
                + "If you cannot find 「添加文档应用」, it means the document owner may need to enable this option, "
                + "or you can try: click **「分享」** (Share) button -> invite the bot app directly.\n"
                + "---"
            )
        return f"Failed: API Error {code} - {msg}"
    return None
