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
agent → participant, while message persistence locks session → agent. Acquiring
session locks from participant updates would invert that order and can deadlock.

## Existing databases

Existing databases do not need to be recreated. When `chat_sessions` lacks
`history_revision`, the DAO returns `None` and paged history bypasses both cache
reads and writes. History continues to read directly from PostgreSQL. Fresh
schemas retain revision-based caching and transactional invalidation unchanged.

`scripts/schema_baseline.sql` remains a fresh-schema initializer, not a migration:
`CREATE TABLE IF NOT EXISTS` does not add the column to an existing table. Its
trigger setup checks the actual table for the column before installing
`chat_history_revision_changed`. If the column is absent, it removes that trigger
instead; the function is inert without it. No table or message data is removed.

If a previous baseline run already installed the broken trigger on a pre-column
database, rerun bootstrap with the corrected code and the existing database
configuration: `uv run python -m app.scripts.bootstrap_db` from `engine/`. This
removes the incompatible trigger and restores message inserts, updates and deletes
without adding a column or recreating the database. Such schemas continue to serve
history uncached after the rerun.
