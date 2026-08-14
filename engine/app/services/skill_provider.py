import base64
import os
import re
from collections.abc import Callable
from typing import NotRequired, TypedDict

import httpx
from fastapi import HTTPException

from app.core.json_types import (
    JsonValue,
    json_object_from,
    json_value_from_response,
    mapping_from_row,
    yaml_load_object,
)

CLAWHUB_BASE = os.getenv("CLAWHUB_BASE", "https://clawhub.ai/api").rstrip("/")
CLAWHUB_MIRROR_BASE = os.getenv("CLAWHUB_MIRROR_BASE", "https://cn.clawhub-mirror.com/api").rstrip("/")
GITHUB_API = "https://api.github.com"
MAX_SKILL_SIZE = 512_000


class SkillFilePayload(TypedDict):
    path: str
    content: str


class SkillFrontmatter(TypedDict, total=False):
    name: str
    description: str


class GitHubUrlParts(TypedDict):
    owner: str
    repo: str
    branch: str
    path: str


class ClawHubSearchResult(TypedDict, total=False):
    slug: str
    displayName: str
    summary: str
    score: int | float
    version: str
    updatedAt: int | float


class ClawHubSkillInfo(TypedDict, total=False):
    displayName: str
    summary: str


class ClawHubOwnerInfo(TypedDict, total=False):
    handle: str


class ClawHubModeration(TypedDict, total=False):
    isSuspicious: bool
    summary: str


class ClawHubPayload(TypedDict, total=False):
    results: list[ClawHubSearchResult]
    skill: ClawHubSkillInfo
    owner: ClawHubOwnerInfo
    moderation: ClawHubModeration | None


class GitHubDirectoryItem(TypedDict):
    name: str
    type: str
    path: str
    url: str
    size: NotRequired[int]


def clawhub_headers_for_base(api_key: str, base_url: str) -> dict[str, str]:
    return {} if "clawhub-mirror.com" in base_url else {"Authorization": f"Bearer {api_key}"} if api_key else {}


def candidate_clawhub_bases(preferred: str | None = None) -> list[str]:
    return list(
        dict.fromkeys(base_url.rstrip("/") for base_url in (preferred, CLAWHUB_BASE, CLAWHUB_MIRROR_BASE) if base_url)
    )


def clawhub_search_endpoint(base_url: str) -> str:
    return f"{base_url}/v1/search" if "clawhub-mirror.com" in base_url else f"{base_url}/search"


def clawhub_download_url(base_url: str) -> str:
    return f"{base_url}/v1/download"


def public_clawhub_url(base_url: str, slug: str) -> str:
    domain = "cn.clawhub-mirror.com" if "clawhub-mirror.com" in base_url else "clawhub.ai"
    return f"https://{domain}/skills/{slug}"


def _parse_search_result(value: JsonValue) -> ClawHubSearchResult | None:
    if not isinstance(value, dict):
        return None
    result: ClawHubSearchResult = {}
    if isinstance(slug := value.get("slug"), str):
        result["slug"] = slug
    if isinstance(display_name := value.get("displayName"), str):
        result["displayName"] = display_name
    if isinstance(summary := value.get("summary"), str):
        result["summary"] = summary
    if isinstance(score := value.get("score"), (int, float)) and not isinstance(score, bool):
        result["score"] = score
    if isinstance(version := value.get("version"), str):
        result["version"] = version
    if isinstance(updated_at := value.get("updatedAt"), (int, float)) and not isinstance(updated_at, bool):
        result["updatedAt"] = updated_at
    return result


def _parse_clawhub_payload(value: JsonValue) -> ClawHubPayload | None:
    if not isinstance(value, dict):
        return None
    payload: ClawHubPayload = {}
    if isinstance(results := value.get("results"), list):
        payload["results"] = [result for item in results if (result := _parse_search_result(item)) is not None]
    skill = value.get("skill")
    if isinstance(skill, dict):
        parsed_skill: ClawHubSkillInfo = {}
        if isinstance(display_name := skill.get("displayName"), str):
            parsed_skill["displayName"] = display_name
        if isinstance(summary := skill.get("summary"), str):
            parsed_skill["summary"] = summary
        payload["skill"] = parsed_skill
    owner = value.get("owner")
    if isinstance(owner, dict):
        parsed_owner: ClawHubOwnerInfo = {}
        if isinstance(handle := owner.get("handle"), str):
            parsed_owner["handle"] = handle
        payload["owner"] = parsed_owner
    moderation = value.get("moderation")
    if moderation is None and "moderation" in value:
        payload["moderation"] = None
    elif isinstance(moderation, dict):
        parsed_moderation: ClawHubModeration = {}
        if isinstance(suspicious := moderation.get("isSuspicious"), bool):
            parsed_moderation["isSuspicious"] = suspicious
        if isinstance(summary := moderation.get("summary"), str):
            parsed_moderation["summary"] = summary
        payload["moderation"] = parsed_moderation
    return payload


async def fetch_clawhub_json(
    path_builder: Callable[[str], str],
    api_key: str = "",
    preferred_base: str | None = None,
    params: dict[str, str] | None = None,
) -> tuple[ClawHubPayload, str]:
    last_error = ""
    for base_url in candidate_clawhub_bases(preferred_base):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    path_builder(base_url), params=params, headers=clawhub_headers_for_base(api_key, base_url)
                )
            if response.status_code == 404:
                last_error = f"ClawHub not found at {base_url}"
            elif response.status_code == 429:
                last_error = f"ClawHub rate limit exceeded at {base_url}"
            elif response.status_code == 200 and "json" in response.headers.get("content-type", ""):
                payload = _parse_clawhub_payload(json_object_from(json_value_from_response(response)))
                if payload is not None:
                    return payload, base_url
                last_error = f"ClawHub API returned invalid JSON payload from {base_url}"
            else:
                last_error = f"ClawHub API error from {base_url}: HTTP {response.status_code}"
        except HTTPException:
            raise
        except Exception as exc:
            last_error = f"Failed to connect to ClawHub at {base_url}: {exc}"
    if "rate limit" in last_error:
        raise HTTPException(429, "ClawHub rate limit exceeded. Please wait a moment and try again.")
    raise HTTPException(502, last_error or "Failed to connect to ClawHub")


async def fetch_clawhub_skill_meta(
    slug: str, api_key: str = "", preferred_base: str | None = None
) -> tuple[ClawHubPayload, str]:
    return await fetch_clawhub_json(
        lambda base_url: f"{base_url}/v1/skills/{slug}", api_key=api_key, preferred_base=preferred_base
    )


def parse_skill_md_frontmatter(content: str) -> SkillFrontmatter:
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    try:
        loaded = yaml_load_object(match.group(1))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    frontmatter: SkillFrontmatter = {}
    loaded_map = mapping_from_row(loaded)
    name = loaded_map.get("name")
    if isinstance(name, str):
        frontmatter["name"] = name
    description = loaded_map.get("description")
    if isinstance(description, str):
        frontmatter["description"] = description
    return frontmatter


def parse_github_url(url: str) -> GitHubUrlParts | None:
    if match := re.match(r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*?)/?$", url):
        return {"owner": match.group(1), "repo": match.group(2), "branch": match.group(3), "path": match.group(4)}
    if match := re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url):
        return {"owner": match.group(1), "repo": match.group(2), "branch": "main", "path": ""}
    return None


async def fetch_github_directory(
    owner: str, repo: str, path: str, branch: str = "main", token: str = ""
) -> list[SkillFilePayload]:
    files: list[SkillFilePayload] = []
    total_size = 0
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def recurse(directory: str, relative_prefix: str, depth: int = 0) -> None:
        nonlocal total_size
        if depth > 3:
            return
        api_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{directory}?ref={branch}"
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            response = await client.get(api_url)
        if response.status_code == 404:
            raise HTTPException(404, f"GitHub path not found: {directory}")
        if response.status_code == 403:
            raise HTTPException(429, "GitHub API rate limit exceeded. Try again later.")
        if response.status_code != 200:
            raise HTTPException(502, f"GitHub API error: {response.status_code}")
        raw_items = json_value_from_response(response)
        items: list[object] = (
            [raw_items] if isinstance(raw_items, dict) else list[object](raw_items) if isinstance(raw_items, list) else []
        )
        if not isinstance(raw_items, (dict, list)):
            raise HTTPException(502, "GitHub API returned invalid payload")
        parsed_items: list[GitHubDirectoryItem] = []
        for item_raw in items:
            if not isinstance(item_raw, dict):
                raise HTTPException(502, "GitHub API returned invalid payload")
            item = json_object_from(item_raw)
            name, item_type, item_path, item_url = item.get("name"), item.get("type"), item.get("path"), item.get("url")
            if (
                not isinstance(name, str)
                or not isinstance(item_type, str)
                or not isinstance(item_path, str)
                or not isinstance(item_url, str)
            ):
                raise HTTPException(502, "GitHub API returned invalid payload")
            parsed_item: GitHubDirectoryItem = {"name": name, "type": item_type, "path": item_path, "url": item_url}
            if "size" in item:
                size = item["size"]
                if not isinstance(size, int) or isinstance(size, bool):
                    raise HTTPException(502, "GitHub API returned invalid payload")
                parsed_item["size"] = size
            parsed_items.append(parsed_item)
        if depth == 0:
            has_skill = any(item["name"].upper() == "SKILL.MD" and item["type"] == "file" for item in parsed_items)
            directory_count = sum(1 for item in parsed_items if item["type"] == "dir")
            if not has_skill:
                if directory_count > 5:
                    raise HTTPException(
                        400,
                        f"This directory contains {directory_count} subdirectories but no SKILL.md. Please provide the URL to a specific skill directory.",
                    )
                raise HTTPException(400, "No SKILL.md found at the root of this directory - not a valid skill package.")
        for item in parsed_items:
            relative_path = f"{relative_prefix}{item['name']}" if relative_prefix else item["name"]
            if item["type"] == "dir":
                await recurse(item["path"], f"{relative_path}/", depth + 1)
            elif item["type"] == "file":
                total_size += item.get("size", 0)
                if total_size > MAX_SKILL_SIZE:
                    raise HTTPException(413, f"Skill exceeds size limit ({MAX_SKILL_SIZE // 1024}KB)")
                async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                    download = await client.get(item["url"])
                if download.status_code == 200:
                    raw_file_obj = json_value_from_response(download)
                    if not isinstance(raw_file_obj, dict):
                        raise HTTPException(502, "GitHub API returned invalid payload")
                    raw_file = json_object_from(raw_file_obj)
                    encoded = raw_file.get("content", "")
                    if not isinstance(encoded, str):
                        raise HTTPException(502, "GitHub API returned invalid payload")
                    files.append(
                        {"path": relative_path, "content": base64.b64decode(encoded).decode("utf-8", errors="replace")}
                    )

    try:
        await recurse(path, "")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Failed to fetch files from GitHub: {exc}") from exc
    return files
