"""Drain pending web_search_events to an S3-compatible landing zone."""

from __future__ import annotations

import gzip
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from app.config import get_settings
from app.core.logging import logger
from app.dao.web_search_event_dao import web_search_event_dao
from app.records.web_search_event import WebSearchEventRecord

_SAFE_INSTANCE = re.compile(r"[^A-Za-z0-9._-]+")
_CLAIM_LIMIT = 500
_STALE = timedelta(minutes=15)
_MAX_BYTES = 8 * 1024 * 1024


class AnalyticsObjectSink(Protocol):
    async def put(self, *, bucket: str, key: str, body: bytes, content_type: str) -> None: ...


class BotoAnalyticsSink:
    """Single-shot PUT via boto3 (not the workspace storage backend)."""

    async def put(self, *, bucket: str, key: str, body: bytes, content_type: str) -> None:
        import asyncio

        def _put() -> None:
            import boto3

            settings = get_settings()
            kwargs: dict[str, object] = {"region_name": settings.S3_REGION or None}
            if settings.S3_ENDPOINT_URL:
                kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL
            if settings.S3_ACCESS_KEY_ID:
                kwargs["aws_access_key_id"] = settings.S3_ACCESS_KEY_ID
            if settings.S3_SECRET_ACCESS_KEY:
                kwargs["aws_secret_access_key"] = settings.S3_SECRET_ACCESS_KEY
            client = boto3.client("s3", **kwargs)
            client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)

        await asyncio.to_thread(_put)


def _sink() -> AnalyticsObjectSink:
    return BotoAnalyticsSink()


def analytics_bucket() -> str:
    settings = get_settings()
    return (settings.ANALYTICS_S3_BUCKET or settings.S3_BUCKET).strip()


def analytics_prefix() -> str:
    prefix = get_settings().ANALYTICS_S3_PREFIX.strip().strip("/")
    return prefix or "web-search"


def object_key(*, now: datetime, instance_id: str, object_id: str) -> str:
    prefix = analytics_prefix()
    if prefix == "agents" or prefix.startswith("agents/"):
        prefix = "web-search"
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_instance = _SAFE_INSTANCE.sub("-", instance_id) or "engine"
    return (
        f"{prefix}/dt={now.astimezone(UTC).date().isoformat()}/"
        f"hour={now.astimezone(UTC).strftime('%H')}/"
        f"{stamp}_{safe_instance}_{object_id}.jsonl.gz"
    )


def event_to_export_row(
    record: WebSearchEventRecord, *, raw_query: str | None
) -> dict[str, object]:
    occurred = record.occurred_at.isoformat() if record.occurred_at else None
    return {
        "event_id": str(record.id),
        "occurred_at": occurred,
        "tenant_id": str(record.tenant_id) if record.tenant_id else None,
        "agent_id": str(record.agent_id) if record.agent_id else None,
        "kind": record.kind,
        "http_status": record.http_status,
        "status_class": record.status_class,
        "latency_ms": record.latency_ms,
        "query_hash": record.query_hash,
        "query_normalized": record.query_normalized,
        "query_raw": raw_query,
        "primary_domain": record.primary_domain,
        "depth": record.depth,
        "output_type": record.output_type,
        "result_count": record.result_count,
        "key_id": str(record.key_id) if record.key_id else None,
        "schema_version": record.schema_version,
    }


def build_jsonl_gz(rows: list[dict[str, object]]) -> bytes:
    payload = "".join(json.dumps(row, separators=(",", ":"), default=str) + "\n" for row in rows)
    return gzip.compress(payload.encode("utf-8"))


async def drain_pending_exports(*, sink: AnalyticsObjectSink | None = None) -> int:
    """Claim pending rows, PUT one object, mark exported. Returns exported count."""
    settings = get_settings()
    if not settings.WEB_SEARCH_ANALYTICS_EXPORT_ENABLED:
        return 0
    bucket = analytics_bucket()
    if not bucket:
        logger.warning("web search export enabled but ANALYTICS_S3_BUCKET/S3_BUCKET is empty")
        return 0

    now = datetime.now(UTC)
    claimed = await web_search_event_dao.claim_pending_export(
        now=now, limit=_CLAIM_LIMIT, stale_after=_STALE
    )
    if not claimed:
        return 0

    include_raw = settings.WEB_SEARCH_ANALYTICS_INCLUDE_RAW
    raw_map = await web_search_event_dao.payloads_for([row.id for row in claimed]) if include_raw else {}
    rows: list[dict[str, object]] = []
    ids: list = []
    for record in claimed:
        raw = raw_map.get(record.id) if include_raw else None
        rows.append(event_to_export_row(record, raw_query=raw))
        ids.append(record.id)
        if len(json.dumps(rows, default=str).encode("utf-8")) > _MAX_BYTES:
            break
    leftover = [row.id for row in claimed if row.id not in ids]
    if leftover:
        await web_search_event_dao.release_export(leftover)

    body = build_jsonl_gz(rows)
    key = object_key(now=now, instance_id=settings.INSTANCE_ID, object_id=str(uuid4()))
    writer = sink or _sink()
    try:
        await writer.put(
            bucket=bucket,
            key=key,
            body=body,
            content_type="application/gzip",
        )
    except Exception:
        logger.exception("web search export PUT failed")
        await web_search_event_dao.release_export(ids)
        return 0

    await web_search_event_dao.mark_exported(ids, now=now)
    if include_raw:
        await web_search_event_dao.delete_payloads(ids)
    return len(ids)


async def expire_old_events() -> int:
    days = max(1, get_settings().WEB_SEARCH_ANALYTICS_RETENTION_DAYS)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    return await web_search_event_dao.delete_older_than(cutoff)


async def start_web_search_export_daemon() -> None:
    """Worker loop: drain pending exports and apply retention."""
    import asyncio

    ticks = 0
    while True:
        try:
            if get_settings().WEB_SEARCH_ANALYTICS_EXPORT_ENABLED:
                _ = await drain_pending_exports()
        except Exception:
            logger.exception("web search export drain failed")
        ticks += 1
        if ticks % 240 == 1:
            try:
                deleted = await expire_old_events()
                if deleted:
                    logger.info(f"web search analytics expired {deleted} events")
            except Exception:
                logger.exception("web search analytics retention failed")
        await asyncio.sleep(15)
