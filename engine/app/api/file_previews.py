"""Authenticated, version-bound attachment transport without storage redirects."""

import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import ClassVar, override
from unicodedata import category
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict

from app.core.permissions import check_agent_access
from app.core.security import load_user_from_access_token, security
from app.records.user import UserRecord
from app.services.file_storage_paths import visible_storage_key
from app.services.storage import get_storage_backend, guess_content_type
from app.services.storage_runtime.base import StorageReadLimitExceededError, StorageVersion

MAX_PREVIEW_BYTES = 20 * 1024 * 1024
PREVIEW_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}


class PreviewRoute(APIRoute):
    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()

        async def secured_response(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            response.headers.update(PREVIEW_HEADERS)
            return response

        return secured_response


router = APIRouter(prefix="/agents/{agent_id}/files", tags=["files"], route_class=PreviewRoute)


class PreviewMetadata(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    path: str
    name: str
    size: int
    version_token: str
    content_type: str


@dataclass(frozen=True, slots=True)
class PreviewTarget:
    path: str
    key: str


async def _fresh_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserRecord:
    return await load_user_from_access_token(credentials.credentials, fresh=True)


async def _authorized_target(
    agent_id: uuid.UUID,
    path: str = Query(min_length=1, max_length=4096),
    current_user: UserRecord = Depends(_fresh_user),
) -> PreviewTarget:
    await check_agent_access(current_user, agent_id, fresh=True)
    relative = path.strip()
    if (
        not relative
        or relative.startswith("/")
        or PureWindowsPath(relative).drive
        or "\\" in relative
        or any(category(character) == "Cc" for character in path)
        or ".." in relative.split("/")
    ):
        raise HTTPException(403, "Invalid preview path")
    canonical = str(PurePosixPath(path))
    if canonical == ".":
        raise HTTPException(404, "File not found")
    key, _ = visible_storage_key(agent_id, canonical, current_user.tenant_id)
    return PreviewTarget(canonical, key)


async def _preview_version(target: PreviewTarget) -> StorageVersion:
    try:
        version = await get_storage_backend().get_version_bounded(target.key, MAX_PREVIEW_BYTES)
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise HTTPException(404, "File not found") from exc
    except StorageReadLimitExceededError as exc:
        raise HTTPException(413, "Preview exceeds 20 MiB") from exc
    if not version.exists or version.is_dir:
        raise HTTPException(404, "File not found")
    return version


@router.get("/preview-metadata", response_model=PreviewMetadata)
async def preview_metadata(target: PreviewTarget = Depends(_authorized_target)) -> PreviewMetadata:
    version = await _preview_version(target)
    return PreviewMetadata(
        path=target.path,
        name=PurePosixPath(target.path).name,
        size=version.size,
        version_token=version.token,
        content_type=guess_content_type(target.path),
    )


@router.get("/preview-bytes")
async def preview_bytes(
    version_token: str = Query(min_length=1, max_length=4096),
    target: PreviewTarget = Depends(_authorized_target),
) -> Response:
    version = await _preview_version(target)
    if version.token != version_token:
        raise HTTPException(409, "File version changed")
    try:
        data = await get_storage_backend().read_bytes_bounded(target.key, MAX_PREVIEW_BYTES)
        current = await get_storage_backend().get_version_bounded(target.key, MAX_PREVIEW_BYTES)
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise HTTPException(409, "File version changed") from exc
    except StorageReadLimitExceededError as exc:
        raise HTTPException(413, "Preview exceeds 20 MiB") from exc
    if not current.exists or current.is_dir or current.token != version_token or len(data) != current.size:
        raise HTTPException(409, "File version changed")
    filename = quote(PurePosixPath(target.path).name, safe="")
    return Response(
        data,
        media_type=guess_content_type(target.path),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
