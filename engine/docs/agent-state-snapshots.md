# Agent state snapshots

In S3 storage mode the [workspace mirror](workspace-mirror.md) keeps a fixed set of guest paths in durable storage. Everything else OpenClaw keeps in the agent directory (`<STORAGE_LOCAL_ROOT or AGENT_DATA_DIR>/<agent id>`, bind-mounted at `/home/node/custom-state`) exists only on the host disk: the sessions and their history, the Knowledge vault, auth profiles and client credentials, the gateway token and `openclaw.json`. State snapshots keep one current recovery point of that state in S3, so a start on a new, empty host directory comes back with the same sessions, Knowledge and credentials.

They run only in S3 mode with `AGENT_STATE_SNAPSHOT_ENABLED` and `WORKSPACE_MIRROR_ENABLED` both on (`snapshot_store()` is the mirror's `durable_store()`). There is no table: the object keys carry everything.

Code: `app/services/agent_state_snapshot.py` (keys, digest and walk bound, locks, one-off containers, prefix delete), `agent_state_snapshot_archive.py` (backup report and archive checks), `agent_state_snapshot_tracker.py` (what the worker remembers: published digests, lag, failure backoff), `agent_state_snapshot_backup.py` (a backup's scratch directory and container), `agent_state_snapshot_worker.py` (the `agent_state_snapshot` loop), `agent_state_snapshot_restore.py` (restore at start) and `host_disk_headroom.py` (the [disk headroom check](#host-disk-headroom) all of them share).

## What is covered

Every regular file under the agent directory except:

- the whole `workspace/` tree and every other path the mirror owns (`workspace_mirror_scope.in_scope`);
- the top-level `logs/`, `tmp/` and `plugin-skills/` directories;
- the engine's own `.maraclaw-mirror-generation` and `.maraclaw-rpc*` files;
- SQLite sidecars: `-shm` files never count as a change, and no `-wal`, `-shm` or `-journal` file is ever placed by a restore.

The archive itself is OpenClaw's own: `openclaw backup create --no-include-workspace` of the whole state directory, which also skips `logs/`, `tmp/` and `plugin-skills/`. It can still hold files the mirror owns (OpenClaw 2026.11.10 archives the workspace bootstrap files); a restore never places them.

An agent "has OpenClaw state" when `agents/main/agent/openclaw-agent.sqlite` or `state/openclaw.sqlite` is a regular file in its directory.

## When a snapshot is taken

- **Loop.** The `agent_state_snapshot` loop (worker role) runs every `AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS` (default 600). It pages the same agents as the mirror (OpenClaw agents in `running`, `idle`, `stopped` and `error`, 50 per page, in id order) and handles one agent at a time. An agent without a directory or without OpenClaw state is skipped; its directory is never created. Beat and progress budget: [runtime/AGENTS.md](../app/runtime/AGENTS.md).
- **Changed only.** The digest is a SHA-256 over the tenant token and the sorted path, size and mtime of every covered file, walked without following symlinks. A walk past 100,000 entries or 30 seconds has no digest and is a rejected pass (see Failure backoff): restore placement uses the same entry, depth and time limits (`walk_covered`). The worker also checks the actual archive's entry and depth limits before upload, independently of changes to the live tree. An agent is skipped while its digest equals the one of the last snapshot this process published; after a restart the newest key's digest is used, read with one listing. The published digest is taken after the backup, because the backup writes its own row into `state/openclaw.sqlite`. Running gateways touch their database files within minutes, so a running agent usually gets one snapshot per interval; a stopped agent gets one after it changed.
- **Locks.** In this order, never the reverse: the exclusive setup lock (`setup_workspace_lock`) without waiting, then the snapshot flock `<agents root>/_setup_locks/<agent id>.snapshot.lock` without waiting. Either one busy skips the agent for this pass. An engine turn holds the setup lock shared for the whole gateway call, so an agent is not snapshotted while one runs. Under both locks the agent's row is read again: a deleted agent (no row), a changed tenant, an active OpenClaw upgrade, pending company-transfer cleanup or vanished state skips it. The backup runs under both locks; the checks, the upload and the prune run under the snapshot flock alone. A turn claimed during the backup waits one second for the setup lock, is left pending (`lock_timeout`) and is claimed again by the dispatch reconciler.
- **Backup.** A one-off container of the agent's image (`openclaw_image_id`, else `MARACLAW_OPENCLAW_IMAGE`; never pulled) named `maraclaw-state-snapshot-<agent id>`, labelled `maraclaw.state-snapshot=create` and `maraclaw.state-snapshot.agent_id`, through `docker_capacity.run_docker` at background priority (a busy Docker lane skips the agent). It runs `sh` as entrypoint, so the image's validators and bootstrap do not run, with Docker's `--init`, the image's default user, no network, 1 GiB of memory and swap, one CPU, 128 PIDs and `--rm`. Two `type=bind` mounts, which fail instead of creating a missing source: the agent directory at `/home/node/custom-state` (read-write; the backup records itself in `state/openclaw.sqlite`) and a work directory `<agents root>/_state_snapshots/create/<random>/out` at `/out`. The command is `timeout -s KILL 120 openclaw backup create --no-include-workspace --verify --json --output /out/snapshot.tar.gz`; the Docker CLI call has a 300-second deadline, after which the container is removed. The guest itself is never entered.
- **Checks.** Outside the event loop, before anything is uploaded: the JSON report says `verified`, not `includeWorkspace`, and lists exactly one asset, the state directory `/home/node/custom-state` at `<archive root>/payload/posix/home/node/custom-state`. The archive is at most `AGENT_STATE_SNAPSHOT_MAX_BYTES` and a readable gzip tar; a `-wal` or `-journal` member with bytes in it means a database was not quiescent. Every `*.sqlite` member is extracted (tar filter `data`, 8 GiB in total) and must pass `PRAGMA integrity_check` read-only, with `trusted_schema` off; all checks share a 120-second deadline. In the agent database `agents/main/agent/openclaw-agent.sqlite`, no `session_nodes` row may have a truthy `lifecycleRunId`, `resumeAfterRestart` or `mainRestartRecovery` in `entry_json`, and `session_pending_inputs` must be empty (a missing table is empty). A gateway restored from a snapshot taken mid-run resumes that run, so such a snapshot is useless. A failed check discards the archive, keeps the previous snapshot and starts the failure backoff.
- **Publish.** The agent's row and tenant are checked again, the archive is uploaded as a new key with `require_absent` (`application/gzip`), then every older snapshot key of the agent without a held marker is deleted. The work directory holds secrets and is removed after every attempt.
- **Failure backoff.** A pass that ends `FAILED` (a backup container error, a host disk short of its headroom, any other error, a snapshot key already taken) or `REJECTED` (the archive checks or the walk bound refused it) starts a backoff for that agent in the worker's memory: the next attempt waits two intervals after the first failure, then four, eight and so on up to 24 intervals (20 minutes up to 4 hours at the default), on the monotonic clock. It is checked before the digest and before either lock, so a skipped agent costs neither a walk nor a lock attempt. A pass that ends `UNCHANGED` or `PUBLISHED` clears it; a `BUSY` or `INELIGIBLE` pass neither starts nor extends it. It is not stored anywhere: a worker restart forgets it, and the previous snapshot stays current throughout.
- An agent with state that has gone more than three intervals without a current snapshot in this process logs `[StateSnapshot] agent=<id> has had no current snapshot for <n>s`, so lock starvation is visible.
- At worker start the loop removes containers labelled `maraclaw.state-snapshot=create` and `<agents root>/_state_snapshots/create/`, which an earlier process may have left. A restore removes what an earlier restore of the same agent left.

## Keys

`<S3_PREFIX>/state-snapshots/<agent id>/<YYYYMMDDTHHMMSSffffff>Z_<digest>_<tenant id or none>.tar.gz`, where the digest is 16 hex characters and `none` stands for an agent without a tenant. A held snapshot has an empty `<key>.held` object beside it. UUIDs are lowercase. Anything under the prefix that does not parse is ignored by restore and never pruned.

The first segment is not a UUID, so the managed S3 quota passes these keys through unmetered ([managed-storage-quota.md](managed-storage-quota.md#scope)).

Held keys and their markers are never pruned. To use a held snapshot (for example once the agent's image runs a newer OpenClaw), delete its `.held` marker. It is then an ordinary key: it is restored only while it is the newest unheld snapshot of the agent's tenant and the host has no OpenClaw state, and the next snapshot the worker publishes prunes it.

## Restore

A start that rehydrates (`rehydrate=True`) restores in `AgentManager._apply_guest_state` before `pull_durable_state`, under the exclusive setup lock every `replace_container` caller already holds. It does nothing unless snapshots are on and the host has neither database; then it takes the snapshot flock without waiting. OpenClaw upgrades start with `rehydrate=False` and never restore.

1. The newest key with the agent's current tenant token and no held marker is chosen. None means a fresh agent.
2. The host disk is checked for the floor plus the archive's size, then the archive is downloaded in ranged reads, bounded by `AGENT_STATE_SNAPSHOT_MAX_BYTES`, into `<agents root>/_state_snapshots/restore/<agent id>/<random>/in/`. Storage's `materialize` writes to the process temp directory, which the Docker daemon cannot see.
3. A one-off container of the exact image the start is about to run (`maraclaw-state-restore-<agent id>`, label `maraclaw.state-snapshot=restore`, the same limits, no network) mounts only that work directory, with `HOME=/tmp` and `OPENCLAW_STATE_DIR=/tmp/openclaw-state`. It records `openclaw --version`, then runs `timeout -s KILL 120 openclaw backup restore <archive> --target <stage> --json` and records its exit code.
4. The restore must have exited 0 and reported `ok`; the manifest's `runtimeVersion` must be no newer than the image's version (dotted numbers compared, a pre-release or build suffix ignored); the manifest must list exactly one asset, the state directory `/home/node/custom-state` at `<archive root>/payload/posix/home/node/custom-state`.
5. Placement walks that payload: every regular file that a snapshot covers and that is not a sidecar is hard-linked from the stage (the same filesystem, so modes are kept) where nothing exists yet. Symlinks are skipped, a symlinked parent directory refuses its files, and an existing file is never overwritten. The two state databases are placed last; if placement fails, the files placed are removed again.
6. The start goes on as before (mirror pull, `materialize_agent_files`, gogcli, config write, start). `materialize_agent_files` skips the restored paths, so legacy keys outside the mirror scope never overwrite restored state.

| Outcome | When | Effect |
| --- | --- | --- |
| Transient | The snapshot flock is busy; listing or download fails; a busy Docker lane, a Docker error, a restore killed by its timeout (137); a local I/O error; a held marker that cannot be written; the host disk is short of its [headroom](#host-disk-headroom) before a download or refusal, or the restore reports that it ran out of disk space | `AgentStateRestoreError` (an `OSError`), which `replace_container` turns into `GuestStartPreflightError` with code `runtime_failed`, on a creation as on a replacement: the start fails with nothing placed and the start reconciler retries it. Every other `OSError` of a creation stays `configuration_failed`. |
| Refused | The restore exits non-zero on a disk that has its headroom (archive rejected); the snapshot's runtime is newer than the image's; a version or the manifest does not parse; an asset outside `/home/node/custom-state`; the archive is larger than `AGENT_STATE_SNAPSHOT_MAX_BYTES` | The `.held` marker is written and a warning logged; nothing is placed and the agent starts fresh. The snapshot is kept. |

## Host disk headroom

`host_disk_headroom.require_headroom` is the one check in front of every engine path that stages a large copy on the guest data volume. It reads the free bytes of the volume that holds the path (its nearest existing ancestor when the path is not created yet) and raises `OSError` with `ENOSPC` when they are fewer than `HOST_DISK_HEADROOM_BYTES` plus the write the caller already knows it is about to make. A floor of 0 turns it off. The disk-usage read is a replaceable function, so tests fake the disk.

| Path | Checked | A short disk |
| --- | --- | --- |
| State snapshot worker | Under both locks, before the scratch directory exists (floor only: the archive's size is not known before the backup) | The pass is `FAILED` and backs off: no container runs, no directory is created, the previous snapshot stays current. |
| Restore | After the listing, before the work directory is created and the archive downloaded (floor plus the archive's size); again before writing any held marker | `AgentStateRestoreError`, transient: nothing is placed, no `.held` marker is written and the start retries. |
| `openclaw_update_snapshot.commit_snapshot` (the OpenClaw upgrade cutover and the container recovery snapshot) | Before the agent directory is copied (floor only), after a crashed attempt's partial copy is removed | `OSError` before anything is written, handled as any copy failure is: the cutover rolls back with `cutover_failed` and brings the original guest back, recovery answers 502 `guest_start_failed` before the pin changes. |
| `agent_deletion_archive.publish_deletion_archive` | Before the workspace is copied into the archive's staging directory (floor only) | `OSError` before anything is written; the cleanup job stays pending with the error recorded and the source workspace is untouched. |

The restore container extracts onto the same disk. Reported disk-full errors stay transient even with the preflight check disabled or if free space has recovered by the time the container exits. A headroom shortage also delays any refusal until the host has room again.

## Deletion and company transfer

Both delete the agent's whole `state-snapshots/<agent id>/` prefix whenever the storage backend has an S3 primary, whatever `AGENT_STATE_SNAPSHOT_ENABLED` and `WORKSPACE_MIRROR_ENABLED` say, under the snapshot flock taken without waiting.

- [Deletion cleanup](../app/services/agent_deletion_cleanup.py) deletes it after publishing the deletion archive, still under the setup lock. A busy flock or a storage error leaves the job pending with the error recorded, and the outbox retries it; the job completes only after the delete.
- [Company-transfer cleanup](../app/services/company_transfer_cleanup.py) deletes it after the credential scrub. A busy flock records `snapshot_busy`, a storage error `snapshot_delete_failed`, and cleanup stays pending. The worker later snapshots the scrubbed state under the new tenant. Keys of the old tenant would never be restored anyway, because a restore requires the agent's current tenant.

The delete removes current objects only. On a versioned bucket the noncurrent versions stay until the lifecycle rule below expires them.

## Security

Archives contain secrets: auth profiles, client credentials, the gateway token and `openclaw.json`. The engine does no client-side encryption. The bucket must have default encryption (SSE-S3 or SSE-KMS) and a private bucket policy (Block Public Access on, access for the engine's principal only). Work directories are created 0700 under `<agents root>/_state_snapshots/` and removed after each backup or restore.

## Lifecycle rule

`python -m scripts.s3_lifecycle --apply` also puts the rule `maraclaw-state-snapshot-noncurrent-versions`: filter prefix `<S3_PREFIX>/state-snapshots/`, noncurrent versions expire after 1 day and expired delete markers are removed. It overlaps the agent rule `maraclaw-agent-noncurrent-versions` when that covers the whole prefix; when two expiration policies overlap, S3 honours the shorter one ([AWS: lifecycle conflicts](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-conflicts.html)), so superseded snapshots live one day. With versioning Enabled or Suspended, `--check` fails unless an enabled rule expires every noncurrent version under `state-snapshots/` within a day. Options, exit codes and IAM: [managed-storage-quota.md](managed-storage-quota.md#scope).

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `AGENT_STATE_SNAPSHOT_ENABLED` | `true` | Turns the worker loop and the restore on. They also need S3 storage and `WORKSPACE_MIRROR_ENABLED`. Deletion and transfer delete the prefix regardless. |
| `AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS` | `600` | Seconds between passes, 60 to 86400. |
| `AGENT_STATE_SNAPSHOT_MAX_BYTES` | `1073741824` (1 GiB) | Largest archive published or restored, 1 MiB to 16 GiB. |
| `HOST_DISK_HEADROOM_BYTES` | `1073741824` (1 GiB) | Free bytes to keep on the host volume before a snapshot, restore, upgrade or recovery snapshot, or deletion archive copy; the expected write is added where it is known. 0 turns the check off. |

## Known limits

- One recovery point per agent. A lost host directory loses what changed since the last published snapshot: up to one interval, longer while passes are skipped.
- An agent whose engine turns hold the setup lock at every pass is never snapshotted; only the starvation warning shows it.
- Backoff and the walk bound mean an agent with more than 100,000 state entries is never snapshotted: it is rejected at every attempt and only the starvation warning shows it.
- A snapshot taken while the guest writes on its own (outside an engine turn) can be rejected by the integrity or quiescence checks. It is retried after the failure backoff.
- The quiescence check reads the agent database schema of OpenClaw 2026.11.10 (`session_nodes.entry_json`, `session_pending_inputs`). A runtime that tracks runs elsewhere passes it.
- The one-off containers (1 GiB each: one per worker process, one per restoring start) are outside aggregate memory admission.
- The failure classes are coarse. A local error that never clears, such as an agent directory on another filesystem (hard links fail with `EXDEV`), counts as transient and fails every start until it is fixed. A nonzero exit without evidence of a transient failure or a reported disk-full error is a refusal once the disk has its required headroom.
- A restoring process that dies after placement and before its cleanup leaves the stage, which holds the agent's secrets, in a mode 0700 directory under `_state_snapshots/restore/<agent id>/` until that agent's next restore clears it.
- A worker process sweeps every backup container and the whole `_state_snapshots/create/` tree when it starts, so a second worker-role process on the same Docker daemon or agents root kills a backup in flight; that agent is snapshotted at a later pass.
- Files placed by a restore belong to the engine's user; that works only because the engine image's `maraclaw` user and the guest's `node` user are both uid 1000.
- Measured on macOS Docker Desktop (arm64) with `arm64-openclaw:2026.11.10`; Linux hosts are not verified.
