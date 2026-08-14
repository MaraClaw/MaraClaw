from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from httpx import Response

from app.core.json_types import JsonObject, json_as_str, json_object_from_response, json_value_from_response
from app.core.logging import logger
from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments, ToolArgumentValue


def _httpx_module():
    import httpx

    return httpx


def _httpx_client(*args: object, **kwargs: object):
    return _httpx_module().AsyncClient(*args, **kwargs)


def _response_mapping(response: Response) -> JsonObject:
    return json_object_from_response(response)


def _nested_mapping(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _object_items(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else default


async def _vercel_deploy(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    project_name = _string_argument(arguments, "project_name")
    source_dir_arg = _string_argument(arguments, "source_dir", ".")
    deploy_method = _string_argument(arguments, "deploy_method", "upload")
    github_repo = _string_argument(arguments, "github_repo")
    framework = _string_argument(arguments, "framework")
    production = arguments.get("production") is True

    if not project_name:
        return "❌ Missing required argument 'project_name'."

    token = await agent_tools._get_vercel_token(agent_id, "vercel_deploy")
    if not token:
        return "❌ Vercel Access Token is not configured. Please paste your token in the tool settings."

    headers = {"Authorization": f"Bearer {token}"}
    source_dir_path = ws / source_dir_arg.lstrip("/")
    if not source_dir_path.exists() or not source_dir_path.is_dir():
        source_dir_path = agent_tools.WORKSPACE_ROOT / str(agent_id) / source_dir_arg.lstrip("/")
        if not source_dir_path.exists() or not source_dir_path.is_dir():
            return f"❌ Source directory '{source_dir_arg}' does not exist in workspace."

    async with _httpx_client(timeout=60.0) as client:
        try:
            project_res = await client.get(f"https://api.vercel.com/v9/projects/{project_name}", headers=headers)
            if project_res.status_code == 200:
                logger.info(f"Vercel project '{project_name}' exists.")
            else:
                payload = {"name": project_name}
                if framework:
                    payload["framework"] = framework
                create_res = await client.post("https://api.vercel.com/v9/projects", headers=headers, json=payload)
                if create_res.status_code not in (200, 201):
                    return f"❌ Failed to create Vercel project '{project_name}': {create_res.text}"

            patch_payload = {"ssoProtection": None, "passwordProtection": None}
            patch_res = await client.patch(
                f"https://api.vercel.com/v9/projects/{project_name}", headers=headers, json=patch_payload
            )
            if patch_res.status_code == 200:
                logger.info(f"Successfully disabled deployment protection for project '{project_name}'")
            else:
                logger.warning(f"Failed to disable deployment protection: {patch_res.text}")

            dep_id = None
            dep_url = None
            if deploy_method == "github":
                if not github_repo:
                    return "❌ Argument 'github_repo' (format 'owner/repo') is required when deploy_method='github'."
                link_payload = {"type": "github", "repo": github_repo}
                link_res = await client.post(
                    f"https://api.vercel.com/v9/projects/{project_name}/link", headers=headers, json=link_payload
                )
                if link_res.status_code not in (200, 201, 409):
                    logger.warning(f"Repo linking returned status {link_res.status_code}: {link_res.text}")
                deploy_payload: dict[str, ToolArgumentValue] = {
                    "name": project_name,
                    "gitSource": {"type": "github", "repo": github_repo, "ref": "main"},
                }
                if production:
                    deploy_payload["target"] = "production"
                dep_res = await client.post(
                    "https://api.vercel.com/v13/deployments", headers=headers, json=deploy_payload
                )
                if dep_res.status_code not in (200, 201):
                    return f"❌ Failed to trigger GitHub deployment: {dep_res.text}"
                dep_data = _response_mapping(dep_res)
                dep_id = json_as_str(dep_data.get("id"))
                dep_url = json_as_str(dep_data.get("url"))
            else:
                files_payload: list[ToolArgumentValue] = []
                ignored_dirs = {".git", "node_modules", ".next", "dist", ".vercel", "out", "build"}
                for root, dirs, files in os.walk(source_dir_path):
                    dirs[:] = [d for d in dirs if d not in ignored_dirs]
                    for file in files:
                        file_path = Path(root) / file
                        rel_path = file_path.relative_to(source_dir_path)
                        try:
                            file_bytes = file_path.read_bytes()
                        except Exception as e:
                            logger.warning(f"Could not read file {file_path}: {e}")
                            continue
                        sha1 = hashlib.sha1(file_bytes, usedforsecurity=False).hexdigest()
                        file_size = len(file_bytes)
                        file_headers = {
                            **headers,
                            "Content-Type": "application/octet-stream",
                            "x-vercel-digest": sha1,
                            "x-vercel-size": str(file_size),
                        }
                        upload_res = await client.post(
                            "https://api.vercel.com/v2/files", headers=file_headers, content=file_bytes
                        )
                        if upload_res.status_code not in (200, 201):
                            logger.error(f"Failed to upload file {rel_path}: {upload_res.text}")
                        files_payload.append({"file": str(rel_path), "sha": sha1, "size": file_size})

                deploy_payload = {"name": project_name, "files": files_payload}
                if framework:
                    deploy_payload["projectSettings"] = {"framework": framework}
                if production:
                    deploy_payload["target"] = "production"
                dep_res = await client.post(
                    "https://api.vercel.com/v13/deployments", headers=headers, json=deploy_payload
                )
                if dep_res.status_code not in (200, 201):
                    return f"❌ Failed to trigger upload deployment: {dep_res.text}"
                dep_data = _response_mapping(dep_res)
                dep_id = json_as_str(dep_data.get("id"))
                dep_url = json_as_str(dep_data.get("url"))

            status = "QUEUED"
            max_polls = 60
            for _poll in range(max_polls):
                status_res = await client.get(f"https://api.vercel.com/v13/deployments/{dep_id}", headers=headers)
                if status_res.status_code == 200:
                    status_data = _response_mapping(status_res)
                    status = json_as_str(status_data.get("readyState")) or status
                    dep_url = json_as_str(status_data.get("url")) or dep_url
                    if status in ("READY", "ERROR", "CANCELED"):
                        break
                await asyncio.sleep(2.0)

            quota_summary = await agent_tools._get_vercel_quota_summary(token)
            if status == "READY":
                return (
                    "✅ **Deployment triggered successfully!**\n\n"
                    + f"- **URL**: https://{dep_url}\n"
                    + "- **Status**: READY (Active)\n"
                    + f"- **Project Name**: {project_name}\n"
                    + f"- **Deployment ID**: {dep_id}\n"
                    + "- **Protection Bypass**: Disabled (Automatically turned off for automated debugging)\n\n"
                    + f"{quota_summary}"
                )
            return (
                f"⚠️ **Deployment state**: {status}\n"
                + f"- **URL**: https://{dep_url}\n"
                + f"- **Deployment ID**: {dep_id}\n"
                + "- **Note**: Check build logs using `vercel_get_deploy_logs` to diagnose errors.\n\n"
                + f"{quota_summary}"
            )
        except Exception as e:
            logger.exception("Vercel deployment failed")
            return f"❌ Failed to deploy to Vercel: {e!s}"


async def _vercel_list_deployments(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    project_name = _string_argument(arguments, "project_name")
    if not project_name:
        return "❌ Missing required argument: 'project_name'."
    token = await agent_tools._get_vercel_token(agent_id, "vercel_list_deployments")
    if not token:
        return "❌ Vercel Access Token is not configured."

    headers = {"Authorization": f"Bearer {token}"}
    async with _httpx_client() as client:
        try:
            res = await client.get(f"https://api.vercel.com/v6/deployments?projectId={project_name}", headers=headers)
            if res.status_code == 200:
                deployments = _object_items(_response_mapping(res).get("deployments"))
                if not deployments:
                    return f"No deployments found for project '{project_name}'."
                lines = [f"📋 **Deployments for {project_name}**:"]
                for dep in deployments[:10]:
                    created_at = dep.get("created")
                    if isinstance(created_at, int):
                        created_dt = datetime.fromtimestamp(created_at / 1000, UTC)
                        created_str = created_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                    else:
                        created_str = str(created_at)
                    lines.append(
                        f"- URL: https://{dep.get('url')} | "
                        + f"Status: {dep.get('state')} | "
                        + f"Created: {created_str} | "
                        + f"ID: `{dep.get('uid')}`"
                    )
                return "\n".join(lines)
            return f"❌ Failed to retrieve deployments: {res.text}"
        except Exception as e:
            return f"❌ Error listing deployments: {e}"


async def _vercel_get_deploy_logs(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    deployment_id = _string_argument(arguments, "deployment_id")
    if not deployment_id:
        return "❌ Missing required argument: 'deployment_id'."
    if "https://" in deployment_id:
        deployment_id = deployment_id.replace("https://", "").split("/")[0]

    token = await agent_tools._get_vercel_token(agent_id, "vercel_get_deploy_logs")
    if not token:
        return "❌ Vercel Access Token is not configured."

    headers = {"Authorization": f"Bearer {token}"}
    async with _httpx_client(timeout=30.0) as client:
        try:
            res = await client.get(f"https://api.vercel.com/v2/deployments/{deployment_id}/events", headers=headers)
            if res.status_code == 200:
                events_raw: object = json_value_from_response(res)
                events: list[object] = []
                if isinstance(events_raw, list):
                    events = list(events_raw)
                elif isinstance(events_raw, dict):
                    nested_events = events_raw.get("events", [])
                    if isinstance(nested_events, list):
                        events = list(nested_events)
                if not events:
                    return f"No logs found for deployment '{deployment_id}'."
                log_lines = []
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    payload = _nested_mapping(event.get("payload"))
                    text = payload.get("text", "") or event.get("text", "")
                    if isinstance(text, str) and text:
                        log_lines.append(text.strip())
                content = "\n".join(log_lines[-100:])
                return f"📜 **Logs for deployment {deployment_id} (last 100 lines)**:\n```\n{content}\n```"
            return f"❌ Failed to retrieve logs: {res.text}"
        except Exception as e:
            return f"❌ Error retrieving logs: {e}"
