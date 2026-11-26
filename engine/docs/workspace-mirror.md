# Workspace mirror and file share links

In S3 storage mode a guest's files live in two places: the host directory bind-mounted at `/home/node/custom-state` (`<STORAGE_LOCAL_ROOT or AGENT_DATA_DIR>/<agent id>`) and the agent's prefix in durable storage, which the web app, the quota ledger and rehydration use. The workspace mirror keeps a fixed set of paths in step between the two, in both directions. A file an agent writes reaches durable storage, and a change made in storage (a file edited in the web app, for example) reaches the guest.

With local storage the agent directory is the storage root, so there is nothing to copy and the mirror is idle. It is also idle in S3 mode when `WORKSPACE_MIRROR_ENABLED=false`: `durable_store()` returns `None`, the worker loop only sleeps, and share links are served from the guest file.

Code: `app/services/workspace_mirror.py` (the pass, the locks, the worker and share-link entry points), `workspace_mirror_plan.py` (pure decisions), `workspace_mirror_ops.py` (one operation per path), `workspace_mirror_local.py` (generation marker, contained probes), `workspace_mirror_scope.py` (scope and the contained walk), `workspace_mirror_worker.py` (the loop) and `agent_materialize.py` (the pull before a start). The manifest is table `agent_workspace_mirror_entries`, read and written through `dao/workspace_mirror_entry_dao.py`. The table is part of the fresh baseline, `local_mode` included; no existing-database upgrade is supplied.

## When it runs

- **Worker loop.** The `workspace_mirror` loop (worker role) runs every `WORKSPACE_MIRROR_INTERVAL_SECONDS`. It pages the OpenClaw agents, system agents included, in status `running`, `idle`, `stopped` and `error` (50 per page, in id order) and runs one full pass for each whose state directory exists, up to `WORKSPACE_MIRROR_CONCURRENCY` agents at once and never two of one organization (the next agent whose organization is free takes a free slot). Each pass's budget starts when its agent's slot is granted. A full pass both pushes and pulls. An agent is skipped for this round, and tried again in the next, when an OpenClaw upgrade is active, company-transfer cleanup is pending, the per-agent setup lock stays exclusively held for more than one second, or another mirror pass holds the mirror lock. One agent's failure is logged and the loop goes on. Beat and progress-budget details: [runtime/AGENTS.md](../app/runtime/AGENTS.md).
- **Start and rehydrate.** When a start rehydrates (`rehydrate=True`), `AgentManager._apply_guest_state` calls `agent_materialize.pull_durable_state` under the exclusive setup lock it already holds. That is a pull-only pass: storage's side moves to the guest, nothing is written to storage and no quota is charged. Afterwards `materialize_agent_files` writes the keys the mirror does not own (anything outside the scope below, except the marker and `.openclaw/`). The pull has its own budget, `START_PULL_BUDGET_SECONDS` (120 seconds). If the directory has no finished generation (see [Marker and generation](#marker-and-generation)) and the pull fails or runs out of budget, the start fails with `WorkspaceRestoreError`. The files pulled so far keep their rows, so the next attempt goes on from there. With a finished generation the failure is logged and the start goes on. OpenClaw upgrades start with `rehydrate=False` and skip all of this.
- **Share link.** `POST /api/gateway/file-links` settles the one requested path before it issues a link (see [Share links](#share-links)).

## Scope

One predicate, `workspace_mirror_scope.in_scope`, decides membership for both the durable listing and the guest tree. Paths are relative to the agent directory.

Mirrored:

- the root files `AGENTS.md`, `HEARTBEAT.md`, `soul.md` and `state.json`;
- every file under `workspace/`, `memory/`, `daily_reports/` and `skills/`;
- every file below `media/outbound/`.

Never mirrored:

- any path with a segment that starts with `.` (this covers `.openclaw/`, the generation marker and the `.mirror-*` temporary files);
- any directory named `node_modules`, `__pycache__`, `venv` or `site-packages`;
- file names (compared case-insensitively) matching `*.sqlite`, `*.sqlite3`, `*.db`, `*.sqlite-*`, `*.db-*`, `*-wal`, `*-shm`, `*-journal`, `*.tmp`, `*~`, `*.swp` or `*.part`;
- paths that cannot be encoded as UTF-8, contain a backslash or a control character, or that `normalize_storage_key` would change;
- everything else in the agent directory, including `openclaw.json`.

The walk lists only regular files and skips symlinks, FIFOs, sockets and devices without opening them. It never follows a symlink and never enters a directory outside the scope. Only files are mirrored, so empty directories are not. A tree with more than 20,000 entries, more than 64 levels of directories, or a walk that takes longer than half the pass budget fails the whole pass, because a partial listing would make unlisted files look deleted.

`media/outbound/` and `workspace/` are also the only places a share link can point at.

## Decision rules

The manifest has one row per agent and path: the guest file's inode, size, mtime, ctime and permission bits (`local_mode`) as last seen, the SHA-256 of its bytes, the stored object's etag, size and version token, `skip_reason` (only `too_large`) and `synced_at`. A row is the base for a path only when its generation equals the directory's current generation (see [Marker and generation](#marker-and-generation)). Without a base, nothing counts as deleted.

A guest file counts as changed when it is gone, when its inode, size, mtime or ctime differ from the row, or when the row carries a `skip_reason`. The stored object counts as changed when its etag or size differ from the row, or when it appeared or vanished.

| Base | Guest | Storage | Action |
| --- | --- | --- | --- |
| none | file | none | push (create) |
| none | none | object | pull |
| none | file | object | compare |
| yes | unchanged | unchanged | nothing |
| yes | changed, file present | unchanged | push (update the object, or create it when it is gone) |
| yes | changed, file gone | unchanged | delete the stored object; a missing root file is pulled back instead |
| yes | unchanged | changed, object present | pull |
| yes | unchanged | changed, object gone | delete the guest file |
| yes | changed | changed | guest gone: pull. Object gone: push. Both present: compare |

An edit beats a delete on either side. A push is conditional on the version token the row recorded, a delete of a stored object likewise, and a delete of a guest file only goes through while the file still matches the row. A lost race leaves the path for the next pass.

**The conflict rule.** Compare is the one place two sides disagree. When the bytes are equal the row is recorded and nothing moves. Otherwise storage's version wins: the guest's bytes are first saved as a conflict copy, then storage's bytes replace the guest file. The copy goes to `workspace/conflicts/` and keeps the original relative path, with `" (conflict YYYY-MM-DD HHMMSS)"` (UTC time of the pass) inserted between stem and suffix, so `workspace/report.docx` becomes `workspace/conflicts/workspace/report (conflict 2026-11-19 101500).docx`. A second copy in the same second gets a number after the stamp, up to 20 attempts. The copy is an ordinary workspace file, so a later pass uploads it.

In pull-only mode (the start path) every action that writes to storage is dropped. Compare still runs, because it only writes to the guest directory.

**File modes.** S3 objects carry no mode, so each row also records the guest file's permission bits (`local_mode`) on every push and every recorded pass. A pull that creates a missing guest file looks at the path's row, from any generation. When that row recorded the same stored etag, the file gets `(local_mode & 0o777) | 0o600`: setuid and setgid never come back, and the owner can always read and write it. Otherwise it gets 0644 less the process umask. A pull that replaces an existing guest file keeps that file's mode, and a conflict copy is 0644. Files the mass-delete guard brings back keep their recorded mode.

## Freezes

A frozen path is left untouched for the pass and decided again in the next one. A path freezes when:

- the guest file was modified less than 5 seconds ago, or the stored object differs from the row and was modified less than 5 seconds ago (the object the row recorded, such as one the mirror just uploaded, never waits);
- the guest file has more than one hard link;
- the stored object is larger than the mirror cap, 100 MiB (the largest tier's per-file limit);
- another path differs from it only by case;
- a scope directory (`workspace`, `memory`, `daily_reports`, `skills`, `media`, `media/outbound`) exists as something other than a real directory, which freezes everything below it;
- the walk did not list it but something that is not a regular file, such as a symlink or a blocked parent, sits on the path;
- a guest-side operation on it fails with a path-specific error, such as permission denied on a file or directory the guest locked down, or a conflict name that is too long. Host-wide errors (no space left, disk quota, I/O error, read-only file system, out of memory or file descriptors) stop the pass instead;
- the guest changes it while the mirror works on it (moved, replaced, grown past the cap), the stored object vanishes or grows past the cap, no free conflict name is left, a delete has no recorded version token, or the key has an unresolved storage accounting claim (`StorageKeyPendingError`).

## Mass-delete guard

If a pass would delete at least 20 files on one side, and those deletes are at least half of the agent's manifest rows for the current generation, the mirror reads it as a side that vanished, not a clean-up, and logs a warning. The guard works in both directions:

- Held-back deletes of stored objects (the guest tree vanished) become pulls, so storage restores the guest files.
- Held-back deletes of guest files become uploads, so the guest restores storage. This covers an agent prefix that was wiped, or an engine pointed at the wrong bucket, once anything in scope is listed there again. A pull-only pass at start drops these deletes instead, so the guest keeps its files and the next full pass uploads them.

The pass also drops the manifest rows of the held paths. A path without a row has no base, so later passes only copy it across and never delete it, even when the restore takes several passes (a budget cut, a full storage limit, a provider error).

A member's delete in the web app never reaches the guard. In S3 mode `DELETE /api/agents/{id}/files/content` (a file or a whole folder; `_delete_workspace_file` in `app/api/files.py`) deletes each stored object through the storage facade, and the facade's local fallback (`STORAGE_LOCAL_FALLBACK_ENABLED`, on by default) deletes the guest copy in the same request. The next pass sees both sides gone and drops the rows, so a member's bulk delete is not undone. Empty folders can stay on the guest disk. With the fallback off only storage loses the files, and a delete of 20 or more files that are at least half the agent's mirrored files is restored by the guard: delete in smaller batches, or on the guest side.

## Empty-storage guard

If storage lists nothing in scope for an agent (no listed object and no root file) while the manifest holds rows of the current generation that reached storage, the mirror reads it as a wrong or wiped bucket, not as deletes. The pass takes no action for that agent, in full and in pull-only mode, and logs a warning. A share-link sync for the agent answers 503 `file_sync_busy`. A side effect when the local fallback is off (see the mass-delete guard above): deleting the agent's last stored in-scope file in the web app leaves the guest copy in place. As soon as storage lists anything in scope again (a member uploads a file, or the default-skill push writes `skills/`), this guard ends and the mass-delete guard above holds the guest deletes back.

## Marker and generation

The file `.maraclaw-mirror-generation` in the agent directory holds a UUID (ASCII, at most 64 bytes, written with fsync), followed by ` pending` until a pass of that generation finishes without running out of budget or stopping on an error. Only a finished generation is a base for the start path. A marker written before this state existed (a bare UUID) counts as finished. Each manifest row records the generation of the directory it described. A directory without a valid marker (new, wiped, restored from a backup, or a marker that is missing or garbled) gets a new pending one, and every old row stops being a base. Anything else on the marker path (a directory, a symlink, an unreadable file) is renamed to `.maraclaw-mirror-generation.displaced-<hex>` next to it and never deleted; it is outside the scope. Nothing is then treated as deleted: paths on both sides are compared and paths on one side are copied across. A finished pass deletes the rows of other generations and of paths that left the scope. The marker is outside the scope, so it is never uploaded or restored.

## Quota behaviour

Pushes go through the managed S3 backend, so every mirrored file is charged against `storage_limit_bytes` and refused above `max_file_size_bytes` like any other managed write ([managed-storage-quota.md](managed-storage-quota.md)). Per path:

- **413 `storage_file_too_large`**, or a guest file above 100 MiB (never sent): the row is recorded with `skip_reason` `too_large` and counts as `too_large` in the report. The file is offered again once it changes or an hour has passed.
- **507 `storage_quota_exceeded`**: the rest of the pass's uploads are skipped and a warning is logged. Nothing is recorded, so every later pass tries again. Pulls and deletes continue.
- An unresolved accounting claim on the key freezes that path.
- **503 `storage_accounting_unavailable`** (the quota ledger's lock or database did not answer): never a refusal. The pass skips the rest of its uploads and deletes of stored objects, records nothing for them and tries them again next pass; pulls continue.
- Any other error, such as a provider error or a database error, stops the pass. Paths already finished keep their rows. The worker logs it and retries the agent next round.

Transfers stream. A push hashes the guest file in chunks and then sends it from the same open descriptor; if the file changed while it was sent, the row keeps the stamp that was read and no content hash, so the next pass pushes the file again rather than calling the two sides a conflict. A pull and a conflict write land in a temporary file renamed into place. Neither direction holds a whole file in memory.

Mirroring never blocks the guest from writing to its own disk. The quota limits what reaches storage. The measured workspace disk allowance is a separate soft quota ([guest-containers.md](guest-containers.md#workspace-disk-quota)).

## Locks

Two per-agent flocks, always taken in this order: the setup lock, then the mirror lock. Never the other way round.

- The setup lock is the one `setup_workspace_lock` uses. The mirror takes it shared through `guest_write_section`, which first checks that no OpenClaw upgrade is active, waits at most one second for the lock, then checks that company-transfer cleanup is not pending. Start, stop and replacement hold it exclusively.
- The mirror lock is `<agents root>/_setup_locks/<agent id>.mirror.lock`, held exclusively. The worker does not wait for it (it skips the agent); a share-link sync waits up to 10 seconds.
- `reconcile_agent` takes no locks. The pull in the start path runs under the exclusive setup lock alone, which already keeps the worker and link syncs out.

A pass stops scheduling work after `PASS_BUDGET_SECONDS` (20 seconds, of which the walk may use half); the start-path pull uses `START_PULL_BUDGET_SECONDS` (120 seconds) instead. Paths not reached wait for the next pass.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `WORKSPACE_MIRROR_ENABLED` | `true` | Turns the mirror, the start-path pull and S3-served share links on. Only S3 storage has anything to mirror. |
| `WORKSPACE_MIRROR_INTERVAL_SECONDS` | `60` | Seconds between worker passes, 10 to 3600. |
| `WORKSPACE_MIRROR_CONCURRENCY` | `4` | Agents the worker loop mirrors at once, 1 to 32, never two of the same organization. Each in-flight agent moves one file at a time in chunks, so this also bounds the mirror's memory. |
| `FILE_SHARE_LINK_TTL_SECONDS` | `900` | Lifetime of a share link, 60 to 86400. |
| `PUBLIC_BASE_URL` | empty | Required to mint share links. |

With S3 storage and `STORAGE_LOCAL_FALLBACK_ENABLED`, the facade builds the fallback backend with `promote=not WORKSPACE_MIRROR_ENABLED`, so the legacy local files are not copied into S3 while the mirror is enabled. The mirror itself works on the S3 primary only.

## Known limits

- A stored object larger than 100 MiB is never pulled, and a guest file larger than 100 MiB is never pushed.
- Files settle for about 5 seconds before they move. A share link requested less than 5 seconds after someone else changed the stored object (for example a web app edit) answers 503 `file_sync_busy`; retrying works.
- The first pass of a large agent spans several passes, because a pass stops at 20 seconds.
- S3 objects carry no mode. A file whose stored bytes changed elsewhere (a web app edit, for example) no longer matches the etag its row recorded, so it comes back 0644 after a wiped disk (see File modes under [Decision rules](#decision-rules)).
- The quota ledger counts only current objects, so on a versioned bucket every mirror overwrite or delete leaves a noncurrent version that costs money and is never metered. `python -m scripts.s3_lifecycle` checks for the lifecycle rule that expires them and adds it ([managed-storage-quota.md](managed-storage-quota.md#scope)).
- Guest root files are never deleted from storage; a missing one is restored.
- A pull or a conflict replaces the guest file by rename. A process that keeps the old file open (for example a shell appending to `workspace/out.log`) keeps writing to the replaced file, and those writes are lost. Two short races remain: a guest write between the engine's last check and its rename, and a guest save between the engine's check and its delete of a file that was removed in storage.
- Files and directories the engine creates on the guest disk belong to the engine's user. That works only because the engine image's `maraclaw` user and the guest's `node` user are both uid 1000.
- With a custom `S3_ENDPOINT_URL` (MinIO and similar), presigned URLs come back relative, so `/f/` downloads stream through the engine (a temporary copy per GET, up to 100 MiB) instead of redirecting.
- At rehydrate, stored keys outside the mirror scope (for example a `workspace/data.db` uploaded in the web app) are still copied over the guest's copy, as before the mirror. The mirror never pushes those files, so guest edits to them revert at the next start.
- A glob wipe such as `rm -rf workspace/*` leaves the dot-named marker in place, so only the mass-delete guard holds the deletes back. A wipe of fewer than 20 files, or of less than half the mirrored files, reaches storage as deletes. The same thresholds apply the other way: a wipe of the agent's stored prefix that small reaches the guest as deletes once storage lists anything in scope again.

## Share links

An agent that has to hand a file to something outside its container (a remote MCP tool, a webhook, an email sent by a remote service) asks the engine for a temporary public download URL.

### POST /api/gateway/file-links

Authenticated with the agent's gateway key in `X-Api-Key`. Request body (extra fields are rejected):

- `path`, a string of 1 to 1024 characters. It can be relative to the agent directory (`workspace/report.docx`), an absolute guest path starting with `/home/node/custom-state/`, or a `.openclaw/workspace/...` path, which reads as `workspace/...`. An empty path, a `..` segment, a NUL byte or any other absolute path is refused.

Only a path that is in scope and under `workspace/` or `media/outbound/` can be shared, and the guest path must be a regular file with a single hard link.

Response 200: `url`, `expires_at`, `expires_in_seconds` (`FILE_SHARE_LINK_TTL_SECONDS`), `filename` and `size_bytes`. The URL is `<PUBLIC_BASE_URL>/f/<token>/<percent-encoded filename>`.

Order of work: normalize and check the path, stat the guest file, read `PUBLIC_BASE_URL`, then (with a durable store) sync that one path to storage, then mint the token. The sync applies the same decision rules as a pass, with three differences: a guest file written a moment ago is not held back for settling, a `too_large` row is not backed off, and a guest file whose stored object was deleted is left in place (the request answers 404 and the next pass, which has the mass-delete guard, decides). Without a durable store (local storage, or the mirror disabled) the size is the guest file's size and nothing is synced.

Errors are JSON. Coded ones carry `{"detail": {"code", "message", ...}}`; the 401 detail is a plain string.

| Status | Code | When |
| --- | --- | --- |
| 400 | `invalid_path` | The path cannot name a file inside the agent directory. |
| 400 | `path_not_shareable` | Not under `workspace/` or `media/outbound/`, out of scope, or the file has more than one hard link. |
| 401 | none | `Missing X-Api-Key header` or `Invalid API key`. |
| 404 | `file_not_found` | Nothing at the path, something that is not a regular file there, or (S3) the stored object was deleted. |
| 409 | `file_conflict` | S3 only. A newer stored version replaced the file and the guest's bytes were saved at `conflict_path` (a field of the detail). |
| 409 | update gate code | An OpenClaw upgrade is active (S3 only; see [openclaw-updates.md](openclaw-updates.md)). |
| 413 | `storage_file_too_large` | S3 only. Above the plan's per-file limit, or above 100 MiB. |
| 422 | none | The body failed validation. |
| 423 | `transfer_cleanup_pending` | S3 only. Company-transfer cleanup is pending for the agent. |
| 503 | `public_base_url_not_configured` | `PUBLIC_BASE_URL` is empty. |
| 503 | `file_sync_busy` | S3 only. The mirror lock or the setup lock stayed taken, the path cannot settle yet (see Freezes), or it changed during the sync. |
| 503 | `file_share_unavailable` | The token store (Redis) is unreachable or would not accept a new token. |
| 503 | `storage_accounting_unavailable`, `storage_accounting_pending` | S3 only. Storage quota accounting, see [managed-storage-quota.md](managed-storage-quota.md). |
| 507 | `storage_quota_exceeded` | S3 only. The upload does not fit the agent's storage allowance. |

### GET and HEAD /f/{token}/{filename}

Public: no login, the unguessable token in the URL is the credential. The router is `file_shares.public_router`, mounted without the `/api` prefix. `web-e` proxies `/f` to the engine in dev (Vite) and in production (Caddy).

- The token is `secrets.token_urlsafe(32)`. Redis holds `<REDIS_KEY_PREFIX>:file-share:<sha256 of the token>` with the JSON `{agent_id, path}`, set with `NX` and the TTL (three attempts to find a free token). The raw token is never stored. A download does not use the link up: it stays valid until the TTL runs out. There is no revocation.
- `filename` has to equal the shared file's own name, so tools see a normal name and extension.
- Every unusable token answers the same `404` with `{"detail": "Not found"}`: unknown, expired or unreadable token, a wrong filename, a path that is no longer shareable, an agent that could no longer mint a link (it fails the gateway key check: deleted, stopped, expired or its company disabled; or company-transfer cleanup is pending for it), a file that is gone, not a regular file or hard-linked. If Redis is unreachable the answer is `503` `File sharing is temporarily unavailable`.
- Local storage (or the mirror disabled): the guest file is streamed through contained descriptors. `Content-Length` is its size at open, and no more bytes than that are sent.
- S3: the engine first checks the stored object and answers 404 when it is missing or a directory marker. `HEAD` is then answered from the stored size, never with a redirect. For `GET` the engine asks the store for a presigned URL valid for 60 seconds (`presign_download_url(..., inline=False, expires_in_seconds=60)`). When it is an absolute `http(s)` URL the answer is `302` with `Location`. Otherwise the engine streams a materialized copy. The link serves the stored object, so a later guest edit shows up once the worker has pushed it.
- Headers: `Content-Disposition: attachment` with the file name, `Content-Type` guessed from the name (`application/octet-stream` if unknown), `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox`, `Cache-Control: private, no-store`, `Referrer-Policy: no-referrer`, plus `Content-Length` set to the file's size when the engine streams it or answers `HEAD`. `HEAD` returns the same headers without a body.

Anyone who holds the URL can download the file until it expires. `PUBLIC_BASE_URL` must be an address the remote tool can reach, which means an internet-reachable one: `localhost` or a Docker network name does not work for a tool running elsewhere. For local testing use a tunnel as in [https-tunnel-testing.md](https-tunnel-testing.md).

Tokens stay out of the stack's own logs. The engine's request log line records the route template `/f/{token}/{filename}`, not the token, `uvicorn.access` is set to WARNING, and the bundled Caddy (`web-e/docker/Caddyfile`) deletes `request>uri` from its access and error logs. `file_share_links.py` logs the agent, path, size and TTL only. A TLS terminator, load balancer or CDN in front of the stack (ALB or CloudFront access logs, for example) records the full URL, token included. The token is a bearer secret until it expires (`FILE_SHARE_LINK_TTL_SECONDS`, 900 seconds by default), so restrict who can read those logs, strip the path there, or keep the TTL short.

### The seeded skill

`file-share-links` is a default skill (`skill_seeder.BUILTIN_SKILLS`, `is_default`, category `development`). Its content is `agent_template/skills/file-share-links/SKILL.md`, read at seed time through `TEMPLATE_SKILL_FOLDERS`, so editing that file edits the seeded skill. It tells the agent to call the endpoint with `$MARACLAW_API_BASE` and `$MARACLAW_GATEWAY_API_KEY` right before the tool call, to put the returned `url` in the tool's file or URL field, to share only what the user asked for, never to echo the key, and never to fall back to sending the local path.

New agents get the file through the workspace scaffold. Existing agents get it from `push_default_skills_to_existing_agents` at startup. The system setting `default_skills_sync_hash` stores `{"hash", "files": {"<folder>/<path>": sha256}}`. The hash covers the folder, path and content of every default skill file, so editing a default skill's content triggers a sync at the next start. Per agent file: a missing file is created; one already equal to the new content is left alone; one still equal to what the previous complete sync pushed is replaced with a version-conditional write; any other (the owner edited it, or no earlier record exists) is kept and logged. Files dropped from a default skill are never deleted. One agent's failure is logged and skipped, and the setting is recorded only after every agent succeeded, so the next startup retries.
