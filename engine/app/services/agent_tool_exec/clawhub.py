"""ClawHub search and installation tool helpers."""

import uuid
from pathlib import Path

from app.services import agent_tools

from .registry import ToolArguments


def _is_safe_folder_name(folder_name: str) -> bool:
    return bool(folder_name) and folder_name not in {".", ".."} and "/" not in folder_name and "\\" not in folder_name


async def _search_clawhub(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    """Search the ClawHub skill registry."""
    query = _string_argument(arguments, "query")
    if not query:
        return "Missing required argument 'query'"

    from app.api.skills import _clawhub_search_endpoint, _fetch_clawhub_json, _get_clawhub_key

    tenant_id = await agent_tools._get_agent_tenant_id(agent_id)
    api_key = await _get_clawhub_key(tenant_id)

    try:
        data, _ = await _fetch_clawhub_json(
            _clawhub_search_endpoint,
            api_key=api_key,
            params={"q": query},
        )
    except Exception as error:
        return f"❌ ClawHub search error: {str(error)[:200]}"

    results = data.get("results", [])
    if not results:
        return f"No skills found matching '{query}'."

    lines = [f"Found {len(results)} skill(s) matching '{query}':\n"]
    for result in results:
        name = result.get("displayName") or result.get("slug", "?")
        slug = result.get("slug", "")
        summary_value = result.get("summary")
        summary = summary_value[:120] if isinstance(summary_value, str) else ""
        updated = ""
        updated_at_value = result.get("updatedAt")
        if isinstance(updated_at_value, (int, float)) and not isinstance(updated_at_value, bool) and updated_at_value:
            from datetime import datetime

            try:
                updated_at = datetime.fromtimestamp(updated_at_value / 1000)  # noqa: DTZ006
                updated = f" | Updated: {updated_at.strftime('%Y-%m-%d')}"
            except Exception:
                updated = ""
        lines.append(f"• **{name}** (`{slug}`){updated}")
        if summary:
            lines.append(f"  {summary}")
    lines.append('\nTo install a skill, use: install_skill(source="<slug>")')
    return "\n".join(lines)


async def _install_skill(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    """Install a skill from ClawHub slug or GitHub URL into the agent's workspace."""
    source = _string_argument(arguments, "source")
    if not source:
        return "❌ Missing required argument 'source'. Provide a ClawHub slug (e.g. 'market-research') or a GitHub URL."

    is_url = source.startswith(("http://", "https://"))
    base = ws

    try:
        if is_url:
            from app.api.skills import _fetch_github_directory, _get_github_token, _parse_github_url

            parsed = _parse_github_url(source)
            if not parsed:
                return "❌ Invalid GitHub URL. Expected format: https://github.com/{owner}/{repo} or https://github.com/{owner}/{repo}/tree/{branch}/{path}"

            owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]
            folder_name = path.rstrip("/").split("/")[-1] if path else repo
            if not _is_safe_folder_name(folder_name):
                return "❌ Invalid skill folder name."
            tenant_id = await agent_tools._get_agent_tenant_id(agent_id)
            token = await _get_github_token(tenant_id)
            files = await _fetch_github_directory(owner, repo, path, branch, token)
            if not files:
                return "❌ No files found at the specified URL."
        else:
            slug = source
            if not _is_safe_folder_name(slug):
                return "❌ Invalid skill folder name."
            from app.api.skills import _fetch_clawhub_skill_archive, _fetch_clawhub_skill_meta, _get_clawhub_key

            tenant_id = await agent_tools._get_agent_tenant_id(agent_id)
            api_key = await _get_clawhub_key(tenant_id)
            try:
                _meta, meta_base = await _fetch_clawhub_skill_meta(slug, api_key=api_key)
            except Exception as error:
                return f"Failed to connect to ClawHub: {str(error)[:200]}"

            files, _ = await _fetch_clawhub_skill_archive(slug, api_key=api_key, preferred_base=meta_base)
            if not files:
                return f"❌ No files found for skill '{slug}' in the ClawHub archive."

            folder_name = slug
        skills_root = (base / "skills").resolve()
        skill_dir = (skills_root / folder_name).resolve()
        if not skill_dir.is_relative_to(skills_root):
            return "❌ Invalid skill folder name."
        _ = skill_dir.mkdir(parents=True, exist_ok=True)

        written = []
        for file in files:
            file_path = (skill_dir / file["path"]).resolve()
            if not file_path.is_relative_to(skill_dir):
                continue
            file_path.parent.mkdir(parents=True, exist_ok=True)
            _ = file_path.write_text(file["content"], encoding="utf-8")
            written.append(file["path"])

        return f"✅ Skill '{folder_name}' installed successfully ({len(written)} files written to skills/{folder_name}/).\n\nFiles: {', '.join(written)}"

    except Exception as error:
        return f"❌ Install failed: {str(error)[:300]}"


def _string_argument(arguments: ToolArguments, name: str) -> str:
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""
