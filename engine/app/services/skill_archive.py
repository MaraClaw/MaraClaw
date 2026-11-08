import io
import zipfile
from pathlib import Path

import httpx
from fastapi import HTTPException

from app.core.json_types import http_header
from app.records.skill import SkillSource
from app.services.skill_provider import (
    MAX_SKILL_SIZE,
    SkillFilePayload,
    candidate_clawhub_bases,
    clawhub_download_url,
    clawhub_headers_for_base,
)


def extract_clawhub_zip_files(data: bytes) -> list[SkillFilePayload]:
    files: list[SkillFilePayload] = []
    total_size = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(502, "ClawHub download did not return a valid skill archive") from exc
    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        raw_paths = [Path(info.filename) for info in entries]
        strip_prefix = ""
        if raw_paths:
            first_parts = raw_paths[0].parts
            if len(first_parts) > 1:
                candidate = first_parts[0]
                has_root_skill = any(p.name.upper() == "SKILL.MD" and len(p.parts) == 1 for p in raw_paths)
                has_prefixed_skill = any(
                    len(p.parts) > 1 and p.parts[0] == candidate and p.parts[-1].upper() == "SKILL.MD"
                    for p in raw_paths
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
            files.append({"path": rel, "content": archive.read(info).decode("utf-8", errors="replace")})
    if not any(file["path"].upper() == "SKILL.MD" for file in files):
        raise HTTPException(400, "No SKILL.md found in ClawHub archive - not a valid skill package")
    return files


async def fetch_clawhub_skill_archive(
    slug: str,
    api_key: str = "",
    preferred_base: str | None = None,
    version: str | None = None,
    tag: str | None = None,
) -> tuple[list[SkillFilePayload], str]:
    params = {"slug": slug}
    if version not in (None, ""):
        params["version"] = version
    if tag not in (None, ""):
        params["tag"] = tag
    last_status = 502
    for base_url in candidate_clawhub_bases(preferred_base):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(
                    clawhub_download_url(base_url), params=params, headers=clawhub_headers_for_base(api_key, base_url)
                )
        except httpx.HTTPError:
            continue
        if response.status_code == 429:
            last_status = 429
            continue
        content_type = http_header(response.headers, "content-type")
        if response.status_code == 200 and ("zip" in content_type or response.content.startswith(b"PK")):
            archive_source = SkillSource.public_archive_source(str(response.url))
            return extract_clawhub_zip_files(response.content), archive_source
    raise HTTPException(last_status, "ClawHub archive download failed")
