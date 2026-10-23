# Durable chat attachment references

## Frontend contract

Normal `/ws/chat/{agent_id}` submissions accept optional `attachments`:

```json
{
  "client_submission_id": "a UUID",
  "content": "Review this document",
  "display_content": "Review this document",
  "attachments": [
    {"path": "workspace/uploads/report.docx", "name": "Quarterly report.docx"}
  ]
}
```

Each reference has **only** `path` and `name`, both required strings. Additional
reference fields are rejected. Limits: 10 references, 1–1024 characters per path,
1–255 characters per name. `attachments: []` is equivalent to omission; `null`
is invalid. The array order and exact strings are retained.

`path` is a canonical POSIX path relative to the current agent's storage root,
as returned by file upload (for example, `workspace/uploads/report.docx`). It is
not a host path, URL, storage key prefixed by the agent UUID, or virtual
`enterprise_info` path. Absolute paths (including Windows drive paths),
traversal/dot components, duplicate or trailing separators, backslashes, and
Unicode `Cc` control characters (including NUL, DEL and C1 controls) are rejected.
`name` is a display filename, not a path; separators, `Cc` controls, `.` and `..`
are rejected. Upload filenames containing colons, leading/trailing non-control whitespace,
or Unicode `Cf` format characters (including ZWJ/ZWNJ in emoji and Persian text)
are accepted unchanged in references and preview metadata. Preview storage-key
mapping retains the existing upload/download normalization policy.

New submissions verify that every path is a file in the authorized agent's
configured local/S3/fallback storage. Local references cannot follow symlinks
outside that agent's root, including a symlinked agent root. Validation does
not read file contents or promote fallback objects. A missing file, directory,
invalid reference, or escaping symlink returns `turn_rejected` with
`reason: "invalid_attachments"` and the valid `client_submission_id`; the socket
remains open and no message, quota charge or gateway work is committed. An
invalid/missing submission UUID retains `reason: "invalid_submission_id"` and
takes precedence when both fields are invalid.

`turn_accepted` remains unchanged: `type`, `message_id`, `session_id`, and
`client_submission_id`. `GET /api/agents/{agent_id}/sessions/{session_id}/messages`
returns nonempty references unchanged as `attachments` on the persisted user
message, including after reconnect/reload. Shared-session user `done` events
carry the same field. Legacy messages and empty lists omit the response field.
References are metadata only: existing `content`, `display_content`, and legacy
`file_name` handling are unchanged. Clients still supply whatever model-facing
content their existing upload flow requires.

The server does not accept attachment size, MIME type, version or bytes claims.
Fetch those through the existing authorized preview endpoint when opening a
preview. Preview/download authorization and single-use download tickets are
unchanged. The file can change or disappear after acceptance; history preserves
the reference, not a snapshot or a new capability to access its contents.
Bounded local reads, including previews, refuse symlinks in any path component,
even when their target remains inside the same agent root; they do not follow
symlinks while opening the file. This is stricter than reference existence checks.

## Replay identity

Omitted/empty attachments preserve the exact existing `v1` fingerprint of
`[content, display_content, file_name]`. Nonempty references use `v2`, hashing
`[content, display_content, file_name, [[path, name], ...]]` with the same canonical
JSON encoding and SHA-256. Changing names, paths, order, adding or removing a
reference under the same scoped submission UUID returns `submission_conflict`.
An exact committed replay is freshly authorized but does not revalidate file
existence, charge quota or enqueue again; deleting a file cannot invalidate an
already accepted receipt.

## Schema and existing databases

`scripts/schema_baseline.sql` defines fresh schemas only. It adds
`chat_messages.attachments JSONB NOT NULL DEFAULT '[]'`, an array/count check,
`v1`/`v2` receipt fingerprints, and receipt immutability covering attachments.
Existing rows represented without attachments remain readable by the record
parser; fresh-schema legacy writes use the empty-array default.

**This change does not upgrade an existing database.** Rerunning the baseline
is not an upgrade path: `CREATE TABLE IF NOT EXISTS` does not add this column or
replace the existing receipt constraint. Do not deploy this engine code against
an unchanged existing schema: even legacy message reads select `attachments`.
An operator must separately review and arrange schema/backfill/constraint/trigger
rollout before enabling this code and coordinating frontend rollout. **The
`chat_messages.attachments` column must already exist before any new-image
`Bootstrap` execution**, not just before serving HTTP/WebSocket traffic: Bootstrap
can also read chat messages through the new code. No migration patch, bootstrap
execution, or database mutation is included in this change.
