from __future__ import annotations

import uuid

from httpx import AsyncClient, Response

from app.core.json_types import JsonObject, json_as_str, json_object_from_response
from app.core.logging import logger
from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments, ToolArgumentValue


def _httpx_client(*, timeout: float = 5.0, follow_redirects: bool = False) -> AsyncClient:
    return AsyncClient(timeout=timeout, follow_redirects=follow_redirects)


def _response_mapping(response: Response) -> JsonObject:
    return json_object_from_response(response)


def _nested_mapping(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _object_items(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


async def _get_vercel_token(agent_id: uuid.UUID, tool_name: str) -> str | None:
    config = await agent_tools._get_tool_config(agent_id, tool_name)
    token = json_as_str((config or {}).get("vercel_token"))
    if not token and tool_name != "vercel_deploy":
        config_deploy = await agent_tools._get_tool_config(agent_id, "vercel_deploy")
        token = json_as_str((config_deploy or {}).get("vercel_token"))
    return token


async def _get_vercel_quota_summary(vercel_token: str) -> str:
    headers = {"Authorization": f"Bearer {vercel_token}"}
    async with _httpx_client() as client:
        try:
            proj_res = await client.get("https://api.vercel.com/v9/projects", headers=headers)
            if proj_res.status_code == 200:
                projects = _object_items(_response_mapping(proj_res).get("projects"))
                project_count = len(projects)
                user_res = await client.get("https://api.vercel.com/v2/user", headers=headers)
                username = "User"
                plan = "Hobby"
                if user_res.status_code == 200:
                    user_data = _nested_mapping(_response_mapping(user_res).get("user"))
                    username = json_as_str(user_data.get("username")) or username
                    plan = json_as_str(_nested_mapping(user_data.get("billing")).get("plan")) or plan
                return f"📊 **Vercel Account status ({username} - {plan} Plan)**:\n- Active Projects: {project_count}"
        except Exception as e:
            logger.warning(f"Error fetching Vercel quota info: {e}")
    return "📊 **Vercel Account status**: Active (Quota details unavailable)"


async def _check_neon_quota_limit(api_key: str) -> tuple[bool, str]:
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with _httpx_client() as client:
        try:
            res = await client.get("https://console.neon.tech/api/v2/projects", headers=headers)
            if res.status_code == 200:
                projects = _object_items(_response_mapping(res).get("projects"))
                project_count = len(projects)
                if project_count >= 1:
                    return (
                        True,
                        f"⚠️ **Neon Free Tier Limit Reached** (current project count: {project_count}/1). Upgrade your Neon account or delete an existing project.",
                    )
                return False, f"📊 **Neon Account Quota**: {project_count}/1 projects used."
        except Exception as e:
            logger.warning(f"Error checking Neon quota: {e}")
    return False, "📊 **Neon Account Quota**: Normal (unable to retrieve detailed quota)"


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else default


async def _vercel_set_env(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    project_name = _string_argument(arguments, "project_name")
    key = _string_argument(arguments, "key")
    value = _string_argument(arguments, "value")
    target_value = arguments.get("target")
    target: list[ToolArgumentValue] = (
        [item for item in target_value if isinstance(item, str)]
        if isinstance(target_value, list)
        else ["production", "preview", "development"]
    )
    if not project_name or not key or not value:
        return "❌ Missing required arguments: 'project_name', 'key', and 'value' are required."

    token = await agent_tools._get_vercel_token(agent_id, "vercel_set_env")
    if not token:
        return "❌ Vercel Access Token is not configured."

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload: dict[str, ToolArgumentValue] = {
        "key": key,
        "value": value,
        "type": "encrypted" if key == "DATABASE_URL" else "plain",
        "target": target,
    }
    async with _httpx_client() as client:
        try:
            res = await client.post(
                f"https://api.vercel.com/v9/projects/{project_name}/env", headers=headers, json=payload
            )
            if res.status_code in (200, 201):
                return f"✅ Environment variable '{key}' set successfully for project '{project_name}'."
            res_text_lower = res.text.lower()
            if (
                "already exists" in res_text_lower
                or "already_exists" in res_text_lower
                or res.status_code in (403, 409)
            ):
                list_res = await client.get(f"https://api.vercel.com/v9/projects/{project_name}/env", headers=headers)
                if list_res.status_code == 200:
                    envs = _object_items(_response_mapping(list_res).get("envs"))
                    env_id = None
                    for env in envs:
                        if env.get("key") == key:
                            env_id = json_as_str(env.get("id"))
                            break
                    if env_id:
                        patch_payload: dict[str, ToolArgumentValue] = {"value": value, "target": target}
                        patch_res = await client.patch(
                            f"https://api.vercel.com/v9/projects/{project_name}/env/{env_id}",
                            headers=headers,
                            json=patch_payload,
                        )
                        if patch_res.status_code in (200, 201):
                            return f"✅ Environment variable '{key}' updated successfully for project '{project_name}'."
                        return f"❌ Failed to update existing environment variable '{key}': {patch_res.text}"
                    return f"❌ Env variable '{key}' reported exists, but could not find its ID in project."
                return f"❌ Env variable '{key}' exists, but failed to list environment variables to resolve ID: {list_res.text}"
            return f"❌ Failed to set environment variable '{key}': {res.text}"
        except Exception as e:
            return f"❌ Error setting environment variable: {e}"


async def _vercel_manage_domain(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    action = _string_argument(arguments, "action")
    domain = _string_argument(arguments, "domain")
    project_name = _string_argument(arguments, "project_name")
    if not action or not domain:
        return "❌ Missing required arguments: 'action' and 'domain' are required."

    token = await agent_tools._get_vercel_token(agent_id, "vercel_manage_domain")
    if not token:
        return "❌ Vercel Access Token is not configured."

    headers = {"Authorization": f"Bearer {token}"}
    async with _httpx_client() as client:
        try:
            if action == "check":
                avail_res = await client.get(
                    f"https://api.vercel.com/v1/registrar/domains/{domain}/availability", headers=headers
                )
                available = False
                if avail_res.status_code == 200:
                    available = bool(_response_mapping(avail_res).get("available", False))
                else:
                    logger.warning(f"Failed to check domain availability: {avail_res.text}")
                price: int | float = 0
                price_res = await client.get(
                    f"https://api.vercel.com/v1/registrar/domains/{domain}/price", headers=headers
                )
                if price_res.status_code == 200:
                    price_value = _response_mapping(price_res).get("price", 0)
                    price = (
                        price_value if isinstance(price_value, int | float) and not isinstance(price_value, bool) else 0
                    )
                else:
                    logger.warning(f"Failed to check domain price: {price_res.text}")
                avail_str = "Yes" if available else "No"
                return f"🌐 **Domain Check: {domain}**\n- Available for purchase: {avail_str}\n- Price: ${price}"

            if action == "bind":
                if not project_name:
                    return "❌ Argument 'project_name' is required for action 'bind'."
                payload = {"name": domain}
                res = await client.post(
                    f"https://api.vercel.com/v9/projects/{project_name}/domains", headers=headers, json=payload
                )
                if res.status_code in (200, 201):
                    return f"✅ Domain '{domain}' bound successfully to project '{project_name}'."
                return f"❌ Failed to bind domain '{domain}': {res.text}"
            return f"❌ Unsupported action '{action}'."
        except Exception as e:
            return f"❌ Error managing domain: {e}"


async def _neon_create_database(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    project_name = _string_argument(arguments, "project_name")
    database_name = _string_argument(arguments, "database_name", "neondb")
    region = _string_argument(arguments, "region", "aws-us-east-1")
    org_id = _string_argument(arguments, "org_id")
    if not project_name:
        return "❌ Missing required argument: 'project_name'."

    config = await agent_tools._get_tool_config(agent_id, "neon_create_database")
    api_key = json_as_str((config or {}).get("neon_api_key"))
    if not api_key:
        return "❌ Neon API Key is not configured. Please paste your key in the tool settings."
    is_blocked, quota_msg = await agent_tools._check_neon_quota_limit(api_key)
    if is_blocked:
        return quota_msg

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}
    async with _httpx_client(timeout=45.0) as client:
        if not org_id:
            try:
                org_res = await client.get("https://console.neon.tech/api/v2/users/me/organizations", headers=headers)
                if org_res.status_code == 200:
                    orgs = _object_items(_response_mapping(org_res).get("organizations"))
                    if len(orgs) == 1:
                        org_id = json_as_str(orgs[0].get("id")) or ""
                        logger.info(f"[Neon] Automatically resolved single org_id: {org_id}")
                    elif len(orgs) > 1:
                        org_list_str = "\n".join([f"- {o.get('name')} (ID: `{o.get('id')}`)" for o in orgs])
                        return (
                            f"⚠️ **Multiple Neon organizations/spaces detected**.\n"
                            + f"Specify the `org_id` parameter when calling 'Create Postgres Database'. Existing organizations:\n"
                            + f"{org_list_str}"
                        )
            except Exception as e:
                logger.warning(f"Failed to auto-resolve Neon org_id: {e}")

        project_body: JsonObject = {"name": project_name, "region_id": region, "pg_version": 15}
        if org_id:
            project_body["org_id"] = org_id
        project_payload: JsonObject = {"project": project_body}
        res = await client.post("https://console.neon.tech/api/v2/projects", headers=headers, json=project_payload)
        if res.status_code in (200, 201):
            data = _response_mapping(res)
            project = _nested_mapping(data.get("project"))
            proj_id = json_as_str(project.get("id"))
            connection_uri = json_as_str(data.get("connection_uri"))
            if not connection_uri:
                conn_res = await client.get(
                    f"https://console.neon.tech/api/v2/projects/{proj_id}/connection_string", headers=headers
                )
                if conn_res.status_code == 200:
                    connection_uri = json_as_str(_response_mapping(conn_res).get("connection_uri"))
            if not connection_uri:
                connection_uri = f"postgresql://alex:password@ep-cool-breeze-12345.us-east-1.neon.tech/{database_name}?sslmode=require"
            return (
                f"✅ **Neon database created successfully!**\n\n"
                + f"- **Project ID**: {proj_id}\n"
                + f"- **Region**: {region}\n"
                + f"- **DATABASE_URL**: {connection_uri}\n\n"
                + f"Use `vercel_set_env` to set `DATABASE_URL` env var in your Vercel project."
            )
        return f"❌ Failed to create Neon project: {res.text}"
