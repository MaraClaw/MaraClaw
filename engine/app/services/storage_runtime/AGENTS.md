# app/services/storage_runtime

This package abstracts local, S3-compatible, and migration fallback storage for agent files and workspaces.

## Backend Contract

- Backends implement async primitives from `base.py`: `exists`, `is_file`, `is_dir`, `list_dir`, `read_bytes`, `write_bytes`, `delete`, `delete_tree`, and `stat`.
- Helper APIs include `read_text`, `write_text`, `get_version`, `write_bytes_if_match`, `delete_if_match`, `local_path_for`, and `presign_download_url`.
- `StorageVersion.token` precedence is `version_id`, then `etag`, then `content_hash`, then modified-time/size.
- Conditional write/delete conflicts return `ConditionalWriteResult`; version conflicts are normal outcomes, not exceptions.

## Selection And Fallback

- Use `get_storage_backend()` from `facade.py`; do not construct concrete backends from callers except tests/backend internals.
- The facade caches one process-local backend. Settings changes after first call do not switch backends unless the cache is reset.
- `STORAGE_BACKEND=local` uses `LocalStorageBackend(STORAGE_LOCAL_ROOT or AGENT_DATA_DIR)`.
- `STORAGE_BACKEND=s3` uses `S3StorageBackend`; with `STORAGE_LOCAL_FALLBACK_ENABLED=true`, it wraps S3 primary plus local fallback.
- Fallback storage is read-through migration. Writes go only to primary; deletes hit both.

## Key And Write Safety

- Pass logical storage keys, not raw filesystem paths.
- Normalize keys with `normalize_storage_key` or prefix helpers.
- Local backend still performs resolved-path prefix checks after normalization.
- Use `WriteCondition(version_token=...)` for user-visible or concurrent edits.
- Do not assume local files are authoritative when S3/fallback is enabled.
- `ensure_local_path()` can raise for backends that cannot materialize local files.
