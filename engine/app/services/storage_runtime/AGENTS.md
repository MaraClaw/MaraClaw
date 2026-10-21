# app/services/storage_runtime

This package abstracts local, S3-compatible, and migration fallback storage for agent files and workspaces.

## Backend Contract

- Backends implement async primitives from `base.py`: `exists`, `is_file`, `is_dir`, `list_dir`, `read_bytes`, `write_bytes`, `delete`, `delete_tree`, and `stat`.
- Helper APIs include `read_text`, `write_text`, `get_version`, `write_bytes_if_match`, `delete_if_match`, `local_path_for`, and `presign_download_url`.
- `read_bytes_bounded(key, max_bytes)` measures actual bytes, including growth, with at most one overflow byte. It raises `StorageReadLimitExceededError` or `FileNotFoundError` without a full read/hash/version preflight. Blocking workers own open/GET, read, and close even if the awaiter is cancelled. Fallback bounds both promotion and the primary winner; legacy stays read only.
- `StorageVersion.token` precedence is `version_id`, then `etag`, then `content_hash`, then modified-time/size.
- Conditional write/delete conflicts return `ConditionalWriteResult`; version conflicts are normal outcomes, not exceptions.
- `create_bytes_if_absent(key, data, content_type=None)` atomically publishes complete bytes without replacement. `ok=True` owns the key; `conflict=True` means an occupied key. A conflict need not include `current_version`.
- `WriteCondition(require_absent=True)` delegates to atomic creation. Version-token-only edits and conditional deletes keep their existing behavior.
- Unsupported atomic creation raises `AtomicCreateUnsupportedError`; provider/auth/transport errors propagate. S3 retries `ConditionalRequestConflict` at most three times, then raises `StorageConditionalRaceError`, distinct from an existing-object 412 conflict.
- The locked S3 SDK models `PutObject.IfNoneMatch` as the `If-None-Match` header. Creation always sends `IfNoneMatch="*"` and explicit `ContentType`. GCS S3-compatible creation fails closed until a native precondition is validated; ordinary GCS writes retain explicit content types.

## Selection And Fallback

- Use `get_storage_backend()` from `facade.py`; do not construct concrete backends from callers except tests/backend internals.
- The facade caches one process-local backend. Settings changes after first call do not switch backends unless the cache is reset.
- `STORAGE_BACKEND=local` uses `LocalStorageBackend(STORAGE_LOCAL_ROOT or AGENT_DATA_DIR)`.
- `STORAGE_BACKEND=s3` uses `S3StorageBackend`; with `STORAGE_LOCAL_FALLBACK_ENABLED=true`, it wraps S3 primary plus local fallback.
- Fallback storage is read-through migration. Writes go only to primary; deletes hit both.
- During migration, hold the legacy source read-only outside this wrapper. Legacy objects count as occupied for atomic creation. Every promotion uses primary atomic creation and returns the primary winner, including materialized paths and presigned reads. Repeated primary disappearance is bounded to three promotion attempts.

## Key And Write Safety

- Pass logical storage keys, not raw filesystem paths.
- Normalize keys with `normalize_storage_key` or prefix helpers.
- Local backend still performs resolved-path prefix checks after normalization.
- Use `WriteCondition(version_token=...)` for user-visible or concurrent edits.
- Upload callers must attempt `create_bytes_if_absent` directly, not check `exists` then write. Advance to a new filename only on `conflict`; bound filename attempts. Set response filename/key only after `ok`; never delete a conflict winner or hide provider failures. Use the accepted candidate for content type, extraction, materialization, and response fields.
- Do not assume local files are authoritative when S3/fallback is enabled.
- `ensure_local_path()` can raise for backends that cannot materialize local files.
