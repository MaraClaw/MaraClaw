"""Short-lived public download links for files in an agent's workspace.

`create_link` turns the guest path an agent names into a token URL; `open_share` serves the file behind such a token to
whoever holds it. The token store, the guest-path rules and the durable storage each live in their own module.
"""

import asyncio
import errno
import os
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from fastapi import HTTPException
from starlette.responses import Response, StreamingResponse

from app.config import get_settings
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.company_transfer_cleanup_dao import company_transfer_cleanup_dao
from app.records.agent import AgentRecord
from app.schemas.file_share_links import FileShareLinkResponse
from app.services.coded_errors import coded_error, conflict
from app.services.file_share_tokens import FileShareStoreUnavailableError, issue, resolve
from app.services.platform_service import PublicBaseUrlNotConfiguredError, platform_service
from app.services.storage import guess_content_type
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import MaterializedFile
from app.services.storage_runtime.channel_files import GuestStamp, open_guest_file, stat_guest_file
from app.services.storage_runtime.disposition import content_disposition
from app.services.workspace_mirror import (
    MirrorBusyError,
    MirrorConflictError,
    SyncedFile,
    agent_state_dir,
    durable_store,
    sync_path,
)
from app.services.workspace_mirror_scope import GuestPathError, is_shareable, normalize_guest_path

PRESIGN_SECONDS = 60
_CHUNK_BYTES = 1024 * 1024
# What opening a guest path reports when it is not a plain file: missing, symlink, directory, device, socket, no access.
_NOT_A_FILE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.EACCES})


def _not_a_file(exc: OSError) -> bool:
    """Whether `exc` says the path is not a plain file; the contained open gives no errno to a non-regular file."""
    return exc.errno is None or exc.errno in _NOT_A_FILE_ERRNOS


def _not_shareable() -> HTTPException:
    return coded_error(400, "path_not_shareable", "Only plain files under workspace/ or media/outbound/ can be shared")


def _file_not_found() -> HTTPException:
    return coded_error(404, "file_not_found", "No such file in the agent workspace")


def _filename(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def _gone() -> HTTPException:
    """The one answer for every unusable token, so it never says whether the token ever existed."""
    return HTTPException(status_code=404, detail="Not found")


def _stat_shareable(root: Path, rel: str) -> GuestStamp:
    try:
        stamp = stat_guest_file(root, rel)
    except OSError as exc:
        if _not_a_file(exc):
            raise _file_not_found() from exc
        raise
    if stamp.nlink != 1:
        raise _not_shareable()
    return stamp


async def _sync_to_store(agent: AgentRecord, rel: str) -> SyncedFile:
    try:
        return await sync_path(agent, rel)
    except MirrorBusyError as exc:
        raise coded_error(503, "file_sync_busy", "The workspace is syncing; retry shortly") from exc
    except MirrorConflictError as exc:
        raise conflict(
            "file_conflict",
            "A newer version replaced this file; your copy was saved under the conflict path",
            conflict_path=exc.conflict_path,
        ) from exc
    except FileNotFoundError as exc:
        raise _file_not_found() from exc


async def create_link(agent: AgentRecord, raw_path: str) -> FileShareLinkResponse:
    """A public download link for the agent's workspace file `raw_path`; every refusal is a coded HTTP error."""
    try:
        rel = normalize_guest_path(raw_path)
    except GuestPathError as exc:
        raise coded_error(400, "invalid_path", str(exc)) from exc
    if not is_shareable(rel):
        raise _not_shareable()
    stamp = await asyncio.to_thread(_stat_shareable, agent_state_dir(agent.id), rel)
    try:
        base = await platform_service.get_public_base_url()
    except PublicBaseUrlNotConfiguredError as exc:
        raise coded_error(503, "public_base_url_not_configured", str(exc)) from exc
    size = stamp.size if durable_store() is None else (await _sync_to_store(agent, rel)).size_bytes
    ttl = get_settings().FILE_SHARE_LINK_TTL_SECONDS
    try:
        token, expires_at = await issue(agent.id, rel, ttl)
    except FileShareStoreUnavailableError as exc:
        raise coded_error(503, "file_share_unavailable", "File sharing is temporarily unavailable") from exc
    filename = _filename(rel)
    logger.info("[FileShare] link issued agent={} path={} size={} ttl={}", agent.id, rel, size, ttl)
    return FileShareLinkResponse(
        url=f"{base}/f/{token}/{quote(filename, safe='')}",
        expires_at=expires_at,
        expires_in_seconds=ttl,
        filename=filename,
        size_bytes=size,
    )


def _open_local(root: Path, rel: str) -> tuple[BinaryIO, int]:
    """Open the guest file contained and hand back a stream that owns its own descriptor, with the size to announce."""
    try:
        with open_guest_file(root, rel) as (fd, stamp):
            if stamp.nlink != 1:
                raise _gone()
            return os.fdopen(os.dup(fd), "rb"), stamp.size
    except OSError as exc:
        if _not_a_file(exc):
            raise _gone() from exc
        raise


def _open_materialized(materialized: MaterializedFile) -> tuple[BinaryIO, int]:
    """Open the copy and release an owned one at once: the open handle keeps its bytes readable until it closes."""
    try:
        stream = materialized.path.open("rb")
    finally:
        materialized.close()
    return stream, os.fstat(stream.fileno()).st_size


def _chunks(stream: BinaryIO, size: int) -> Iterator[bytes]:
    """Read at most `size` bytes, so a file that grew after its length was announced cannot overrun it."""
    with stream:
        remaining = size
        while remaining > 0 and (chunk := stream.read(min(_CHUNK_BYTES, remaining))):
            remaining -= len(chunk)
            yield chunk


def _download_headers(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": content_disposition(filename, "attachment"),
        "Content-Type": guess_content_type(filename),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
    }


def _head_response(headers: dict[str, str], size: int) -> Response:
    """What GET would announce for `size` bytes, without a body and never a redirect."""
    return Response(headers={**headers, "Content-Length": str(size)})


def _file_response(stream: BinaryIO, size: int, headers: dict[str, str], *, head: bool) -> Response:
    if head:
        stream.close()
        return _head_response(headers, size)
    return StreamingResponse(_chunks(stream, size), headers={**headers, "Content-Length": str(size)})


async def open_share(token: str, filename: str, *, head: bool) -> Response:
    """The file behind a live share token: streamed, or redirected to a one-minute storage URL. Else 404 or 503."""
    try:
        grant = await resolve(token)
    except FileShareStoreUnavailableError as exc:
        raise HTTPException(status_code=503, detail="File sharing is temporarily unavailable") from exc
    if grant is None or filename != _filename(grant.path) or not is_shareable(grant.path):
        raise _gone()
    # A link lives only while its agent could still mint one: the gateway's own predicate (not stopped, not expired,
    # company enabled) plus no company-transfer cleanup in flight, which the guest-write gate refuses minting for.
    if not await agent_dao.is_openclaw_active(grant.agent_id):
        raise _gone()
    if await company_transfer_cleanup_dao.is_pending(grant.agent_id):
        raise _gone()
    headers = _download_headers(filename)
    store = durable_store()
    if store is None:
        stream, size = await asyncio.to_thread(_open_local, agent_state_dir(grant.agent_id), grant.path)
        return _file_response(stream, size, headers, head=head)
    key = agent_storage_key(grant.agent_id, grant.path)
    # Presigning never touches the object, so a missing one has to be ruled out first to answer the uniform 404.
    version = await store.get_version(key)
    if not version.exists or version.is_dir:
        raise _gone()
    if head:
        return _head_response(headers, version.size)
    url = await store.presign_download_url(key, filename, inline=False, expires_in_seconds=PRESIGN_SECONDS)
    if url is not None and url.startswith(("http://", "https://")):
        return Response(status_code=302, headers={**headers, "Location": url})
    try:
        materialized = await store.materialize(key)
    except FileNotFoundError as exc:
        raise _gone() from exc
    if materialized is None:
        raise _gone()
    stream, size = await asyncio.to_thread(_open_materialized, materialized)
    return _file_response(stream, size, headers, head=head)
