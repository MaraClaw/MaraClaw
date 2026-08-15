# Web search analytics and lakehouse landing

Billed Linkup calls (`POST /v1/search|fetch|research|extract`) are captured in Postgres as `web_search_events` and, when enabled, drained to S3-compatible object storage for Spark / Databricks.

GET polls for async research/extract jobs are **not** counted.

## What is stored

| Place | Query text |
|-------|------------|
| `web_search_events` | Normalized query or **host only** (fetch/extract). HMAC-SHA256 hash. No raw string, no response body. |
| `web_search_export_payloads` | Raw query only when `INCLUDE_RAW` **and** export is enabled **and** a bucket is set. Deleted in the same transaction as `exported`. |
| PA APIs / web-a | Counts, orgs, and top-N `query_normalized`. Never `query_raw`. |
| Logs | Path + status only. Do not log `q` / URL / body. |

Hash key: `WEB_SEARCH_ANALYTICS_HASH_KEY`, falling back to `SECRET_KEY`.

Retention: `WEB_SEARCH_ANALYTICS_RETENTION_DAYS` (default 90). The warehouse is the long-term store.

Events have **no FK** to `agents` / `tenants`. Deleting a company does not wipe history. `tenant_id` stays so gold can still group that org.

## Capture

`app/services/linkup/analytics.py` runs **after** the upstream Linkup response, on a separate connection. A failed insert does not change the guest response.

`WEB_SEARCH_ANALYTICS_CAPTURE_ENABLED` (default true) stops inserts without dropping the table.

The Linkup proxy is **not** bound to `_CRUD_DB` so a 60s search does not pin a pool slot.

## Export (Spark / Databricks)

Off by default. Set:

```
WEB_SEARCH_ANALYTICS_EXPORT_ENABLED=true
ANALYTICS_S3_BUCKET=your-bucket   # or reuse S3_BUCKET
ANALYTICS_S3_PREFIX=web-search/
```

Credentials: existing `S3_*`. Do **not** use `get_storage_backend()` or write under `S3_PREFIX=agents`.

Worker (`PROCESS_ROLE` worker / `start_web_search_export_daemon`) claims `export_state=pending` with `FOR UPDATE SKIP LOCKED`, gzip-compresses JSONL, and PUTs:

```
s3://$BUCKET/web-search/dt=YYYY-MM-DD/hour=HH/{utc}_{instance}_{uuid}.jsonl.gz
```

Partition clock is **export time** (UTC). `occurred_at` is inside each row. Object names are unique; files are never appended. Dedup key for consumers: `event_id`.

Each JSON line includes `schema_version` (starts at `1`). Add columns additively; bump the version when a field meaning changes.

IAM: write-only on `web-search/`. Bucket not public. PUTs set `ServerSideEncryption=AES256`.

## Lakehouse (not in this repo)

| Layer | Object | Grain | Notes |
|-------|--------|-------|--------|
| Bronze | Autoloader / Spark read of `web-search/dt=*/hour=*/*.jsonl.gz` | file of events | Land as-is. Dedup `event_id`. |
| Silver | `fact_web_search` (Delta / Iceberg) | 1 row / event | Types, `occurred_date`. Do not expect response bodies in v1. |
| Gold | `gold_search_daily_org` | date × tenant × kind | Volume, error rate, quota rate, latency. |
| Gold | `gold_search_trending` | date × (tenant?) × query_hash | Hits, distinct agents, distinct orgs. |
| Gold | `gold_org_search_profile` | tenant | 7d/30d volume, WoW growth, deep-research share, quota hits. |

Databricks Autoloader example:

```python
(
    spark.readStream.format("cloudFiles")
    .option("cloudFiles.format", "json")
    .option("pathGlobFilter", "*.jsonl.gz")
    .load("s3://YOUR_BUCKET/web-search/")
    .dropDuplicates(["event_id"])
)
```

### Business gold (decisions, not engine SQL)

| Decision | Signal |
|----------|--------|
| Upsell capacity / higher plan | Org volume, quota `status_class`, deep `depth` share, research/extract mix |
| Upsell extraction product | High `fetch`+`extract` vs `search` |
| Expansion | WoW/MoM event growth per tenant |
| New vertical agent / dataset | Platform `query_normalized` / `primary_domain` clusters shared by many orgs |
| Which Linkup SKU to buy | Kind mix + `output_type` + latency |

## Product ACL

`GET /api/admin/search-analytics/*` is **platform_admin** only. Org admins do not get a v1 UI.

- **Entire system:** omit `tenant_id`. Summary `scope` is `system`. Trending ranks by how many companies share a query, then by hits.
- **One org:** pass `tenant_id`. Summary `scope` is `org`.

Per-org is also a warehouse dimension (`tenant_id` on every event). `scope=platform` is still accepted on trending but is no longer required.

See `docs/admin-apis.md`.
