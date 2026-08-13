"""Skills API — global skill registry CRUD."""

import asyncio
import io
import uuid as _uuid
import zipfile
from pathlib import Path
from typing import NotRequired, TypedDict

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.core.security import get_current_admin, get_current_user, require_role
from app.dao.skill_dao import skill_dao, skill_file_dao
from app.db.session import connection_ctx
from app.db.types import as_jsonb
from app.records.skill import SkillRecord
from app.records.user import UserRecord
from app.services.skill_provider import (
    MAX_SKILL_SIZE,
    ClawHubModeration,
    ClawHubOwnerInfo,
    ClawHubPayload,
    ClawHubSkillInfo,
    SkillFilePayload,
    candidate_clawhub_bases as _candidate_clawhub_bases,
    clawhub_download_url as _clawhub_download_url,
    clawhub_headers_for_base as _clawhub_headers_for_base,
    clawhub_search_endpoint as _clawhub_search_endpoint,
    fetch_clawhub_json as _fetch_clawhub_json,
    fetch_clawhub_skill_meta as _fetch_clawhub_skill_meta,
    fetch_github_directory as _fetch_github_directory,
    parse_github_url as _parse_github_url,
    parse_skill_md_frontmatter as _parse_skill_md_frontmatter,
    public_clawhub_url as _public_clawhub_url,
)

router = APIRouter(prefix="/skills", tags=["skills"])


class TenantTokenPayload(TypedDict):
    token: str


class SavedSkillResult(TypedDict):
    id: str
    name: str
    folder_name: str
    tier: NotRequired[int]
    is_suspicious: NotRequired[bool]
    moderation_summary: NotRequired[str]
    has_scripts: NotRequired[bool]
    file_count: NotRequired[int]
    source: NotRequired[str]
    archive_source: NotRequired[str]


async def _get_tenant_setting(tenant_id: str | None, key: str) -> str:
    """Resolve a tenant setting value: tenant_settings DB > empty."""
    if tenant_id:
        try:
            async with connection_ctx() as conn:
                row = await conn.fetchone(
                    "SELECT value FROM tenant_settings WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
                    {"tenant_id": _uuid.UUID(tenant_id), "key": key},
                )
                if row and isinstance(row.get("value"), dict):
                    token = row["value"].get("token")
                    if isinstance(token, str):
                        return token
        except Exception as error:
            logger.warning(f"[Skills] Failed to retrieve tenant setting: {error}")
    return ""


async def _get_github_token(tenant_id: str | None = None) -> str:
    """Resolve GitHub token from tenant settings DB."""
    return await _get_tenant_setting(tenant_id, "github_token")


async def _get_clawhub_key(tenant_id: str | None = None) -> str:
    """Resolve ClawHub API key from tenant settings DB."""
    return await _get_tenant_setting(tenant_id, "clawhub_key")


def _extract_clawhub_zip_files(data: bytes) -> list[SkillFilePayload]:
    """Convert a ClawHub skill zip into [{"path", "content"}] records."""
    files: list[SkillFilePayload] = []
    total_size = 0

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(502, "ClawHub download did not return a valid skill archive") from exc

    entries = [info for info in archive.infolist() if not info.is_dir()]
    raw_paths = [Path(info.filename) for info in entries]
    strip_prefix = ""
    if raw_paths:
        first_parts = raw_paths[0].parts
        if len(first_parts) > 1:
            candidate = first_parts[0]
            has_root_skill = any(p.name.upper() == "SKILL.MD" and len(p.parts) == 1 for p in raw_paths)
            has_prefixed_skill = any(
                len(p.parts) > 1 and p.parts[0] == candidate and p.parts[-1].upper() == "SKILL.MD" for p in raw_paths
            )
            all_share_prefix = all(len(p.parts) > 1 and p.parts[0] == candidate for p in raw_paths)
            if not has_root_skill and has_prefixed_skill and all_share_prefix:
                strip_prefix = f"{candidate}/"

    for info in entries:
        rel = info.filename.lstrip("/")
        if strip_prefix and rel.startswith(strip_prefix):
            rel = rel[len(strip_prefix) :]
        path = Path(rel)
        if not rel or path.is_absolute() or ".." in path.parts:
            continue

        total_size += info.file_size
        if total_size > MAX_SKILL_SIZE:
            raise HTTPException(413, f"Skill exceeds size limit ({MAX_SKILL_SIZE // 1024}KB)")

        content = archive.read(info).decode("utf-8", errors="replace")
        files.append({"path": rel, "content": content})

    if not any(f["path"].upper() == "SKILL.MD" for f in files):
        raise HTTPException(400, "No SKILL.md found in ClawHub archive — not a valid skill package")
    return files


async def _fetch_clawhub_skill_archive(
    slug: str,
    api_key: str = "",
    preferred_base: str | None = None,
    version: str | None = None,
    tag: str | None = None,
) -> tuple[list[SkillFilePayload], str]:
    """Download a ClawHub skill archive from official API or mirror."""
    params = {"slug": slug}
    if version:
        params["version"] = version
    if tag:
        params["tag"] = tag

    last_error = ""
    for base_url in _candidate_clawhub_bases(preferred_base):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(
                    _clawhub_download_url(base_url),
                    params=params,
                    headers=_clawhub_headers_for_base(api_key, base_url),
                )
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 404:
                last_error = f"Skill '{slug}' not found on ClawHub at {base_url}"
                continue
            if resp.status_code == 429:
                last_error = f"ClawHub rate limit exceeded at {base_url}"
                continue
            if resp.status_code == 200 and ("zip" in content_type or resp.content.startswith(b"PK")):
                return _extract_clawhub_zip_files(resp.content), base_url
            last_error = f"ClawHub download failed from {base_url}: HTTP {resp.status_code}"
        except HTTPException:
            raise
        except Exception as exc:
            last_error = f"Failed to download ClawHub skill from {base_url}: {exc}"
    if "rate limit" in last_error:
        raise HTTPException(429, "ClawHub rate limit exceeded. Please wait a moment and try again.")
    raise HTTPException(502, last_error or f"Failed to download skill '{slug}' from ClawHub")


class SkillFileIn(BaseModel):
    path: str
    content: str


class SkillCreateIn(BaseModel):
    name: str
    description: str = ""
    category: str = "custom"
    icon: str = "📋"
    folder_name: str
    files: list[SkillFileIn] = []


class ClawhubInstallIn(BaseModel):
    slug: str


class UrlImportIn(BaseModel):
    url: str


# ─── Helpers ──────────────────────────────────────────


def classify_portability(content: str) -> int:
    """Classify skill portability: 1=pure prompt, 2=CLI/API, 3=OpenClaw native."""
    openclaw_markers = [
        "bash pty:",
        "process action:",
        "Clawdbot",
        "exec tool",
        "openclaw.json",
        "imessage tool",
        "slack tool",
    ]
    cli_markers = [
        "requires:",
        "bins:",
        "env:",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "python3",
        "brew ",
        "pip install",
        "npm install",
        "curl ",
    ]
    lower = content.lower()
    for kw in openclaw_markers:
        if kw.lower() in lower:
            return 3
    for kw in cli_markers:
        if kw.lower() in lower:
            return 2
    return 1


def _ensure_skill_write_access(skill: SkillRecord, current_user: UserRecord):
    """Allow platform admins to edit everything; tenant admins can edit
    tenant-owned skills AND builtin (preset) skills visible to their tenant.
    Builtin skills are treated as presets -- placed during company init,
    but fully manageable by org_admin afterwards.
    """
    if current_user.role == "platform_admin":
        return
    if not current_user.tenant_id:
        raise HTTPException(403, "Cannot modify skills without a tenant")
    # Allow org_admin to manage: their own tenant skills OR builtin (preset) skills
    if skill.tenant_id is not None and skill.tenant_id != current_user.tenant_id:
        raise HTTPException(403, "Cannot modify other-tenant skills")


async def _save_skill_to_db(
    folder_name: str,
    name: str,
    description: str,
    category: str,
    icon: str,
    files: list[SkillFilePayload],
    source_url: str | None = None,
    tenant_id: str | None = None,
) -> SavedSkillResult:
    """Create a Skill + SkillFile records in the database."""
    tid = _uuid.UUID(tenant_id) if tenant_id else None
    existing = await skill_dao.get_by_folder_name(folder_name, tenant_id=tid, tenant_scoped=True)
    if existing:
        raise HTTPException(
            409,
            f"A skill with folder name '{folder_name}' already exists. Delete it first or use a different name.",
        )

    skill = await skill_dao.create(
        obj_in={
            "name": name,
            "description": description,
            "category": category,
            "icon": icon,
            "folder_name": folder_name,
            "is_builtin": False,
            "tenant_id": tid,
        }
    )

    for f in files:
        content = f["content"].replace("\x00", "")
        await skill_file_dao.create(obj_in={"skill_id": skill.id, "path": f["path"], "content": content})

    return {"id": str(skill.id), "name": skill.name, "folder_name": skill.folder_name}


# ─── ClawHub Integration ─────────────────────────────


@router.get("/clawhub/search")
async def search_clawhub(q: str, current_user: UserRecord = Depends(get_current_user)):
    """Proxy search requests to the ClawHub API."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    api_key = await _get_clawhub_key(tenant_id)
    data, _ = await _fetch_clawhub_json(
        _clawhub_search_endpoint,
        api_key=api_key,
        params={"q": q},
    )
    results = data.get("results", [])
    return [
        {
            "slug": r.get("slug"),
            "displayName": r.get("displayName"),
            "summary": r.get("summary"),
            "score": r.get("score"),
            "version": r.get("version"),
            "updatedAt": r.get("updatedAt"),
        }
        for r in results
    ]


@router.get("/clawhub/detail/{slug}")
async def clawhub_detail(slug: str, current_user: UserRecord = Depends(get_current_user)):
    """Fetch full metadata for a skill from ClawHub."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    api_key = await _get_clawhub_key(tenant_id)
    try:
        data, _ = await _fetch_clawhub_skill_meta(slug, api_key=api_key)
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to connect to ClawHub: {e}") from e


@router.post("/clawhub/install")
async def install_from_clawhub(body: ClawhubInstallIn, current_user: UserRecord = Depends(get_current_user)):
    """Install a skill from ClawHub into the global registry."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    slug = body.slug

    # 1. Fetch metadata from ClawHub (with retry for rate limits)
    api_key = await _get_clawhub_key(tenant_id)
    meta: ClawHubPayload | None = None
    meta_base = None
    for attempt in range(3):
        try:
            meta, meta_base = await _fetch_clawhub_skill_meta(slug, api_key=api_key)
            break
        except HTTPException:
            raise
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            raise HTTPException(502, f"Failed to connect to ClawHub: {e}") from e

    if meta is None:
        raise HTTPException(502, "Failed to retrieve ClawHub metadata")

    skill_info: ClawHubSkillInfo = meta.get("skill", {})
    owner_info: ClawHubOwnerInfo = meta.get("owner", {})
    moderation: ClawHubModeration = meta.get("moderation") or {}

    handle = owner_info.get("handle", "").lower()
    if not handle:
        raise HTTPException(400, "Could not determine skill owner handle from ClawHub")

    # 2. Build result with moderation warning
    is_suspicious = moderation.get("isSuspicious", False)
    moderation_summary = moderation.get("summary", "")

    # 3. Fetch files from the ClawHub archive
    files, archive_base = await _fetch_clawhub_skill_archive(slug, api_key=api_key, preferred_base=meta_base)

    if not files:
        raise HTTPException(404, "No files found in the ClawHub skill archive")

    # 4. Extract name/description from SKILL.md
    skill_md = next((f for f in files if f["path"].upper() == "SKILL.MD"), None)
    if not skill_md:
        raise HTTPException(400, "No SKILL.md found — not a valid skill package")

    frontmatter = _parse_skill_md_frontmatter(skill_md["content"])
    name = frontmatter.get("name", skill_info.get("displayName", slug))
    description = frontmatter.get("description", skill_info.get("summary", ""))

    # 5. Classify portability tier
    tier = classify_portability(skill_md["content"])
    tier_labels = {1: "clawhub-tier1", 2: "clawhub-tier2", 3: "clawhub-tier3"}
    has_scripts = any("/" in f["path"] for f in files if f["path"] != "SKILL.md")

    # 6. Save to DB
    result = await _save_skill_to_db(
        folder_name=slug,
        name=name,
        description=description,
        category=tier_labels.get(tier, "clawhub"),
        icon="",
        files=files,
        source_url=_public_clawhub_url(archive_base, slug),
        tenant_id=tenant_id,
    )

    result["tier"] = tier
    result["is_suspicious"] = is_suspicious
    result["moderation_summary"] = moderation_summary
    result["has_scripts"] = has_scripts
    result["file_count"] = len(files)
    result["source"] = "clawhub"
    result["archive_source"] = archive_base
    return result


@router.post("/import-from-url")
async def import_from_url(body: UrlImportIn, current_user: UserRecord = Depends(get_current_user)):
    """Import a skill from any GitHub URL into the global registry."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    token = await _get_github_token(tenant_id)
    parsed = _parse_github_url(body.url)
    if not parsed:
        raise HTTPException(
            400, "Invalid GitHub URL. Expected format: https://github.com/{owner}/{repo}/tree/{branch}/{path}"
        )

    owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]

    # Fetch files
    files = await _fetch_github_directory(owner, repo, path, branch, token=token)
    if not files:
        raise HTTPException(404, "No files found at the specified path")

    # Validate SKILL.md exists
    skill_md = next((f for f in files if f["path"].upper() == "SKILL.MD"), None)
    if not skill_md:
        raise HTTPException(400, "No SKILL.md found at this URL — not a valid skill package")

    frontmatter = _parse_skill_md_frontmatter(skill_md["content"])
    name = frontmatter.get("name", path.rstrip("/").split("/")[-1] if path else repo)
    description = frontmatter.get("description", "")

    # Derive folder_name from the last path segment
    folder_name = path.rstrip("/").split("/")[-1] if path else repo

    tier = classify_portability(skill_md["content"])
    tier_labels = {1: "url-import-tier1", 2: "url-import-tier2", 3: "url-import-tier3"}

    result = await _save_skill_to_db(
        folder_name=folder_name,
        name=name,
        description=description,
        category=tier_labels.get(tier, "url-import"),
        icon="",
        files=files,
        source_url=body.url,
        tenant_id=tenant_id,
    )

    result["tier"] = tier
    result["file_count"] = len(files)
    result["source"] = "url"
    return result


@router.post("/import-from-url/preview")
async def preview_url_import(body: UrlImportIn, current_user: UserRecord = Depends(get_current_user)):
    """Preview what will be imported from a GitHub URL without saving."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    token = await _get_github_token(tenant_id)
    parsed = _parse_github_url(body.url)
    if not parsed:
        raise HTTPException(400, "Invalid GitHub URL format")

    owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]

    files = await _fetch_github_directory(owner, repo, path, branch, token=token)
    if not files:
        raise HTTPException(404, "No files found at the specified path")

    skill_md = next((f for f in files if f["path"].upper() == "SKILL.MD"), None)
    if not skill_md:
        raise HTTPException(400, "No SKILL.md found — not a valid skill package")

    frontmatter = _parse_skill_md_frontmatter(skill_md["content"])
    tier = classify_portability(skill_md["content"])

    return {
        "name": frontmatter.get("name", path.rstrip("/").split("/")[-1] if path else repo),
        "description": frontmatter.get("description", ""),
        "tier": tier,
        "files": [{"path": f["path"], "size": len(f["content"])} for f in files],
        "total_size": sum(len(f["content"]) for f in files),
        "has_scripts": any("/" in f["path"] for f in files if f["path"] != "SKILL.md"),
    }


# ─── Standard CRUD ────────────────────────────────────


@router.get("/")
async def list_skills(current_user: UserRecord = Depends(get_current_user)):
    """List global skills scoped by tenant (builtin + tenant-specific)."""
    skills = await skill_dao.list_for_tenant_scope(current_user.tenant_id)
    return [
        {
            "id": str(s.id),
            "name": s.name,
            "description": s.description,
            "category": s.category,
            "icon": s.icon,
            "folder_name": s.folder_name,
            "is_builtin": s.is_builtin,
            "is_default": s.is_default,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in skills
    ]


@router.get("/{skill_id}")
async def get_skill(skill_id: str, current_user: UserRecord = Depends(get_current_user)):
    """Get a skill with its files."""
    skill = await skill_dao.get_for_tenant_scope(
        skill_id,
        tenant_id=current_user.tenant_id,
        role=current_user.role,
        with_files=True,
    )
    if not skill:
        raise HTTPException(404, "Skill not found")
    return {
        "id": str(skill.id),
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "icon": skill.icon,
        "folder_name": skill.folder_name,
        "is_builtin": skill.is_builtin,
        "files": [{"path": f.path, "content": f.content} for f in skill.files],
    }


@router.post("/")
async def create_skill(body: SkillCreateIn, current_user: UserRecord = Depends(get_current_admin)):
    """Create a custom skill."""
    skill = await skill_dao.create(
        obj_in={
            "name": body.name,
            "description": body.description,
            "category": body.category,
            "icon": body.icon,
            "folder_name": body.folder_name,
            "is_builtin": False,
            "tenant_id": current_user.tenant_id,
        }
    )

    if not body.files:
        await skill_file_dao.create(
            obj_in={
                "skill_id": skill.id,
                "path": "SKILL.md",
                "content": (
                    f"---\nname: {body.name}\ndescription: {body.description}\n---\n\n"
                    f"# {body.name}\n\n## Overview\n{body.description}\n"
                ),
            }
        )
    else:
        for f in body.files:
            await skill_file_dao.create(obj_in={"skill_id": skill.id, "path": f.path, "content": f.content})

    return {"id": str(skill.id), "name": skill.name}


class SkillUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    icon: str | None = None
    files: list[SkillFileIn] | None = None


@router.put("/{skill_id}")
async def update_skill(skill_id: str, body: SkillUpdateIn, current_user: UserRecord = Depends(get_current_admin)):
    """Update a skill's metadata and/or files."""
    skill = await skill_dao.get_for_tenant_scope(
        skill_id,
        tenant_id=current_user.tenant_id,
        role=current_user.role,
        with_files=True,
    )
    if not skill:
        raise HTTPException(404, "Skill not found")
    _ensure_skill_write_access(skill, current_user)

    updates = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.category is not None:
        updates["category"] = body.category
    if body.icon is not None:
        updates["icon"] = body.icon
    if updates:
        skill = await skill_dao.update(db_obj=skill, obj_in=updates) or skill

    if body.files is not None:
        await skill_dao.replace_files(skill.id, [(f.path, f.content) for f in body.files])

    return {"id": str(skill.id), "name": skill.name}


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, current_user: UserRecord = Depends(get_current_admin)):
    """Delete a skill (not builtin)."""
    skill = await skill_dao.get_for_tenant_scope(
        skill_id,
        tenant_id=current_user.tenant_id,
        role=current_user.role,
    )
    if not skill:
        raise HTTPException(404, "Skill not found")
    _ensure_skill_write_access(skill, current_user)
    await skill_dao.delete_with_files(skill.id)
    return {"ok": True}


# ─── Tenant GitHub Token Settings ───────────────────────────


class SkillSettingsIn(BaseModel):
    github_token: str | None = None
    clawhub_key: str | None = None


async def _upsert_tenant_setting(tenant_id, key: str, value: str):
    """Helper to upsert a tenant setting."""
    token_payload: TenantTokenPayload = {"token": value}
    setting_value: JsonObject = {"token": token_payload["token"]}

    async with connection_ctx() as conn:
        existing = await conn.fetchone(
            "SELECT tenant_id FROM tenant_settings WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
            {"tenant_id": tenant_id, "key": key},
        )
        if existing:
            await conn.execute(
                "UPDATE tenant_settings SET value = %(value)s, updated_at = NOW() "
                "WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
                {"tenant_id": tenant_id, "key": key, "value": as_jsonb(setting_value)},
            )
        else:
            await conn.execute(
                "INSERT INTO tenant_settings (tenant_id, key, value) VALUES (%(tenant_id)s, %(key)s, %(value)s)",
                {"tenant_id": tenant_id, "key": key, "value": as_jsonb(setting_value)},
            )


def _mask_token(token: str) -> str:
    if token and len(token) > 8:
        return f"{token[:4]}...{token[-4:]}"
    return "****" if token else ""


@router.get("/settings/token")
async def get_skill_token_status(
    current_user=Depends(require_role("org_admin", "platform_admin")),
):
    """Check if GitHub token and ClawHub key are configured for this tenant."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    gh_token = await _get_github_token(tenant_id)
    ch_key = await _get_clawhub_key(tenant_id)
    return {
        "configured": bool(gh_token),
        "source": "tenant" if tenant_id else "env",
        "masked": _mask_token(gh_token),
        "clawhub_configured": bool(ch_key),
        "clawhub_masked": _mask_token(ch_key),
    }


@router.put("/settings/token")
async def set_skill_token(
    body: SkillSettingsIn,
    current_user=Depends(require_role("org_admin", "platform_admin")),
):
    """Save GitHub token and/or ClawHub key for this tenant.

    Accessible by org_admin (to manage their own company's credentials) and
    platform_admin. require_role performs exact-match checks, so both roles
    must be listed explicitly.
    """
    if not current_user.tenant_id:
        raise HTTPException(400, "No tenant associated")

    if body.github_token is not None:
        await _upsert_tenant_setting(current_user.tenant_id, "github_token", body.github_token)
    if body.clawhub_key is not None:
        await _upsert_tenant_setting(current_user.tenant_id, "clawhub_key", body.clawhub_key)
    return {"ok": True}


# ─── Path-based browse endpoints for FileBrowser ───────────


@router.get("/browse/list")
async def browse_list(path: str = "", current_user: UserRecord = Depends(get_current_user)):
    """List skill folders (root) or files/subdirs within a skill folder."""
    if not path or path == "/":
        skills = await skill_dao.list_for_tenant_scope(current_user.tenant_id)
        return [{"name": s.folder_name, "path": s.folder_name, "is_dir": True, "size": 0} for s in skills]

    clean = path.strip("/")
    folder = clean.split("/")[0]
    skill = await skill_dao.get_by_folder_for_tenant_scope(
        folder,
        tenant_id=current_user.tenant_id,
        role=current_user.role if current_user.role == "platform_admin" else "org_admin",
        with_files=True,
    )
    if not skill:
        return []

    sub = clean[len(folder) :].strip("/")

    items = []
    seen_dirs: set[str] = set()
    for f in skill.files:
        if sub:
            if not f.path.startswith(sub + "/"):
                continue
            remainder = f.path[len(sub) + 1 :]
        else:
            remainder = f.path

        if "/" in remainder:
            dir_name = remainder.split("/")[0]
            if dir_name not in seen_dirs:
                seen_dirs.add(dir_name)
                dir_path = f"{folder}/{sub}/{dir_name}" if sub else f"{folder}/{dir_name}"
                items.append({"name": dir_name, "path": dir_path, "is_dir": True, "size": 0})
        else:
            file_path = f"{folder}/{f.path}"
            items.append({"name": remainder, "path": file_path, "is_dir": False, "size": len(f.content.encode())})

    return items


@router.get("/browse/read")
async def browse_read(path: str, current_user: UserRecord = Depends(get_current_user)):
    """Read a file from a skill folder."""
    parts = path.strip("/").split("/", 1)
    if len(parts) < 2:
        raise HTTPException(400, "Path must include folder and file")
    folder, file_path = parts
    skill = await skill_dao.get_by_folder_for_tenant_scope(
        folder,
        tenant_id=current_user.tenant_id,
        role=current_user.role if current_user.role == "platform_admin" else "org_admin",
        with_files=True,
    )
    if not skill:
        raise HTTPException(404, "Skill not found")
    for f in skill.files:
        if f.path == file_path:
            return {"content": f.content}
    raise HTTPException(404, "File not found")


class BrowseWriteIn(BaseModel):
    path: str
    content: str


@router.put("/browse/write")
async def browse_write(body: BrowseWriteIn, current_user: UserRecord = Depends(get_current_admin)):
    """Write a file in a skill folder. Creates the skill if the folder doesn't exist."""
    parts = body.path.strip("/").split("/", 1)
    if len(parts) < 2:
        raise HTTPException(400, "Path must include folder and file")
    folder, file_path = parts
    skill = await skill_dao.get_by_folder_for_tenant_scope(
        folder,
        tenant_id=current_user.tenant_id,
        role=current_user.role,
        with_files=True,
    )
    if not skill:
        skill = await skill_dao.create(
            obj_in={
                "name": folder.replace("-", " ").title(),
                "description": "",
                "category": "custom",
                "icon": "--",
                "folder_name": folder,
                "is_builtin": False,
                "tenant_id": current_user.tenant_id,
            }
        )
        skill.files = []
    else:
        _ensure_skill_write_access(skill, current_user)

    existing = None
    for f in skill.files:
        if f.path == file_path:
            existing = f
            break
    if existing:
        await skill_file_dao.update(db_obj=existing, obj_in={"content": body.content})
    else:
        await skill_file_dao.create(obj_in={"skill_id": skill.id, "path": file_path, "content": body.content})
    return {"ok": True}


@router.delete("/browse/delete")
async def browse_delete(path: str, current_user: UserRecord = Depends(get_current_admin)):
    """Delete a file or an entire skill folder."""
    parts = path.strip("/").split("/", 1)
    folder = parts[0]
    skill = await skill_dao.get_by_folder_for_tenant_scope(
        folder,
        tenant_id=current_user.tenant_id,
        role=current_user.role,
        with_files=True,
    )
    if not skill:
        raise HTTPException(404, "Skill not found")
    _ensure_skill_write_access(skill, current_user)

    if len(parts) == 1:
        await skill_dao.delete_with_files(skill.id)
    else:
        await skill_file_dao.delete_by_skill_and_path(skill.id, parts[1])
    return {"ok": True}
