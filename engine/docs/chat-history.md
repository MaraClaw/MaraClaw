# Paged chat history

`GET /api/agents/{agent_id}/sessions/{session_id}/messages/page?limit=50&cursor=...`
returns `{items, next_cursor, has_more}`. Pages select the newest rows first by
`(created_at, id)` and return projected entries in ascending order; inline-tool
entries belonging to one row stay together. The legacy `/messages` array API is
unchanged. Authorization runs before every cache lookup.

## Cache freshness and locking

`app/services/chat_history.py` caches projected pages for 30 seconds. The
transactional `chat_history_revision_changed` trigger in
`scripts/schema_baseline.sql` changes `chat_sessions.history_revision` to a fresh
random UUID for message inserts, updates, deletes and both sides of conversation
reassignment. Committed message mutations invalidate even cached older pages.

Sender display-name changes do not update session revisions; cached names can
remain stale until their existing 30-second TTL expires. There is deliberately
no participant-to-session invalidation trigger: agent updates lock
agent → participant, while message persistence locks agent → session
(`chat_session_dao.lock_for_agent` takes the agent row `FOR KEY SHARE` first, the
order native claims and settlements use). Acquiring session locks from participant
updates would add a participant → session order that message persistence does not
follow.

## Existing databases

Existing databases do not need to be recreated. When `chat_sessions` lacks
`history_revision`, the DAO returns `None` and paged history bypasses both cache
reads and writes. History continues to read directly from PostgreSQL. Fresh
schemas retain revision-based caching and transactional invalidation unchanged.

`scripts/schema_baseline.sql` remains a fresh-schema initializer, not a migration,
apart from the guarded `chat_history_changes.recorded_at` upgrade (see Journal
retention). `CREATE TABLE IF NOT EXISTS` does not add `history_revision` to an
existing table, so the trigger setup checks the actual table for the column before
installing `chat_history_revision_changed`. If the column is absent, it removes
that trigger instead; the function is inert without it. No table or message data
is removed.

If a previous baseline run already installed the broken trigger on a pre-column
database, rerun bootstrap with the corrected code and the existing database
configuration: `uv run python -m app.scripts.bootstrap_db` from `engine/`. This
removes the incompatible trigger and restores message inserts, updates and deletes
without adding a column or recreating the database. Such schemas continue to serve
history uncached after the rerun.

`ix_chat_messages_agent_conv_created` on `(agent_id, conversation_id,
created_at DESC)` serves agent-scoped conversation reads
(`chat_message_dao.list_recent`, `list_for_agent_conversation`) in order, so a
recent-history read stops after its page instead of sorting the whole
conversation. A bootstrap rerun builds it on an existing database with a plain
`CREATE INDEX`, which blocks writes to `chat_messages` while it runs; on a large
table, build it first with `CREATE INDEX CONCURRENTLY IF NOT EXISTS` and the
baseline definition. Add no dependency statistics on `agent_id` and
`conversation_id`: the planner costs a `BitmapAnd` of the single-column indexes
as if both were independent, and an accurate estimate makes that plan, with its
full-conversation sort, look cheaper.

## Journal retention

Every message insert, update and delete adds one `chat_history_changes` row, and
only a session cascade removes it otherwise. `recorded_at` is the write time: the
`refresh_chat_history_revision` trigger stamps `clock_timestamp()`, and rows that
predate the column all carry the time of the bootstrap that added it, so they
become purgeable `CHAT_HISTORY_RETENTION_DAYS` after the upgrade. The worker-role
loop `chat_history_retention` (`app/services/chat_history_retention_worker.py`)
calls `chat_message_dao.purge_history_changes`, which deletes up to 5,000 rows
older than the window per statement: picked in `ix_chat_history_changes_recorded_at`
order with `FOR UPDATE SKIP LOCKED`, deleted by `ctid`, with no lock on
`chat_sessions`. Full batches run back to back; otherwise the loop polls every 60
seconds. A database error backs off 1 to 30 seconds and retries. The default is 30
days; `0` keeps every row and the loop stays ready.

A client holding a pruned revision gets the existing reset page; `unchanged`
and deltas inside the window are unaffected.

A bootstrap rerun on an existing database adds the column without rewriting the
table (the default is stable) and builds `ix_chat_history_changes_recorded_at`
with a plain `CREATE INDEX`, which blocks writes to `chat_history_changes` while it
runs. On a large journal, run both statements before the deploy:

```sql
ALTER TABLE chat_history_changes ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chat_history_changes_recorded_at ON chat_history_changes (recorded_at);
```

Bootstrap then skips both for this table (checked on PostgreSQL 18.6).
