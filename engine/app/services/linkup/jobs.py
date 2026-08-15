"""Bind async Linkup research/extract jobs to the creating key."""

from __future__ import annotations

from uuid import UUID

from app.dao.linkup_api_key_dao import linkup_api_key_dao, linkup_async_job_dao
from app.records.linkup_api_key import LinkupApiKeyRecord


class LinkupJobKeyRemovedError(Exception):
    """The key that created this async job was deleted."""


async def bind_job(*, upstream_job_id: str, key_id: UUID, kind: str) -> None:
    existing = await linkup_async_job_dao.get_by_job_id(upstream_job_id)
    if existing is not None:
        return
    _ = await linkup_async_job_dao.create(
        obj_in={"upstream_job_id": upstream_job_id, "key_id": key_id, "kind": kind}
    )


async def key_for_job(upstream_job_id: str) -> LinkupApiKeyRecord:
    job = await linkup_async_job_dao.get_by_job_id(upstream_job_id)
    if job is None:
        raise LinkupJobKeyRemovedError("Unknown Linkup async job")
    record = await linkup_api_key_dao.get(job.key_id)
    if record is None:
        raise LinkupJobKeyRemovedError("Linkup API key for this job was removed")
    return record
