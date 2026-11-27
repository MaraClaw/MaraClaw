# app/db

Live psycopg3 data-access layer. SQLAlchemy is gone from `app/`.

## Status

- This package **is** the runtime path. There is no `app/database.py` shim.
- Do **not** import SQLAlchemy here (`check_no_new_sqlalchemy.py` forbids `app/db/`).

## Layout

| Module | Role |
|---|---|
| `url.py` | Accept native `postgresql://` / `postgres://` URLs or libpq keyword conninfo; reject driver schemes and the `ssl` query alias (use `sslmode`) |
| `pool.py` | `init_pool` / `close_pool` / `ping_pool` / `set_request_lock_timeout` / `pool_stats`; request pool built with `max_waiting` and a `reset` hook |
| `connection.py` | `DbConnection` (`execute`, `fetch*`, maps errors) |
| `session.py` | `connection_ctx` / `get_db` / `bind_crud_connection` / `optional_connection_ctx` / `transaction` / `durable_transaction` / `run_after_commit` |
| `errors.py` | `DbError`, `UniqueViolationError`, `ForeignKeyViolationError`, `CheckViolationError`, `LockNotAvailableError` (`NOWAIT` / `lock_timeout`), `PoolQueueFullError`, `LockWaitTimeoutError` (a `LockNotAvailableError` **and** a `DbOverloadError`) |
| `types.py` | `dict_row`, `as_jsonb` |

## Conventions

- Named `%(name)s` params only. Nested `transaction()` joins (no savepoints). Top-level `connection_ctx` flushes deferred `aclver` bumps after commit.
- `run_after_commit(on_commit, on_rollback=None)` queues synchronous callbacks on the owning top-level `connection_ctx` or `transaction(db=...)`. `on_commit` runs after the commit and the deferred flushes (also after `flush_request_transaction`); `on_rollback` runs when the commit is not reached, including on cancellation. Nested joins share the outer queue, raw savepoints are not callback-aware, and with no open scope `on_commit` runs at once. A failing callback is logged and the rest still run.
- `app.runtime.lifecycle.Runtime.start()` opens the pool **before** bootstrap seeders and role-gated workers/connectors. `get_pool()` raises if unset.
- Pool hygiene: `DATABASE_POOL_MAX_IDLE` (600s) / `DATABASE_POOL_MAX_LIFETIME` (1800s) plus a cheap `closed` check on checkout - not `SELECT 1`.
- `durable_transaction()` commits on a connection of `durable_pool()` (a second pool opened on first use from the main pool's settings, closed by `close_pool`) and never joins the request transaction. Use it only when a commit must be durable before the caller proceeds (managed storage reservations). Code holding a durable connection must never wait on the main pool.
- Overload protection on the request pool: `DATABASE_POOL_MAX_WAITING` (60; 0 = unbounded) caps the checkout queue, so the next request gets psycopg_pool `TooManyRequests` at once; `DATABASE_POOL_TIMEOUT` still bounds a queued wait (`PoolTimeout`). `app.main` answers both, `QueryCanceled` and a lock-wait timeout with 503 + `Retry-After`. `pool_stats()` returns a `PoolStats` per open pool (the request pool as `main`, the durable pool once opened) from that pool's own `get_stats()`: size, available connections, waiting requests and the request counters. It checks out no connection and takes no lock, so the metrics scrape (`../core/metrics.py`) never queues behind the saturation it reports.
- Request lock timeout: `bind_crud_connection` sets `DATABASE_REQUEST_LOCK_TIMEOUT_MS` (5000; 0 disables) as a **session-level** `lock_timeout` (`set_config(..., false)`; never `SET LOCAL`, which a mid-request `flush_request_transaction` commit would drop) and the pool's `reset` clears it before reuse. It is never pool-wide: workers, the durable pool and bootstrap keep none, and the quota ledger's own `SET LOCAL lock_timeout` still applies inside its transaction. Only a timeout expiry becomes `LockWaitTimeoutError`, so `except LockNotAvailableError` busy handlers behave as before. Both a timeout and `NOWAIT` are SQLSTATE 55P03; `errors._is_lock_timeout` tells them apart by the error's source function (`diag.source_function`, `ProcessInterrupts` for a timeout), which the server never translates, unlike the message that follows `lc_messages`. The English message is read only when the server sent no source function.
- `get_db()` exists. Short CRUD routers bind `bind_crud_connection` from `app.main` so DAOs share one checkout.
- Do **not** attach `bind_crud_connection` to websocket, connector inbound, gateway, or AgentBay control (LLM / long I/O), nor to a route that calls Docker, a CLI or object storage: mount it in a second unbound router (see `api/AGENTS.md`) and read/write in explicit short `connection_ctx()` / `transaction()` scopes, never holding a row lock across the external call.
- Uninitialized pool: `bind_crud_connection` is a no-op (ASGI unit tests).

## Tests

- `tests/test_db_url.py`, `test_db_connection.py`, `test_db_session.py`, `test_db_overload_http.py` (the pool-queue case needs `MARACLAW_TEST_POSTGRES_DSN`) - otherwise no live Postgres. `test_db_cancellation_postgres.py` covers the real lock-timeout / reset behavior, including the source function of a real timeout, a real `NOWAIT` refusal and a PL/pgSQL-raised 55P03 that carries the timeout's English text. The PostgreSQL image has no message catalogs (built without NLS), so a translated message is covered with an error built from the translated text and the `ProcessInterrupts` source function in `test_db_connection.py` and `test_db_overload_http.py`.
- The database-free CI gate does not apply `../../scripts/schema_baseline.sql`; `lifecycle-postgres` does through the opted-in `../../tests/test_bootstrap_postgres.py` bootstrap tests (`BOOTSTRAP_TEST_DATABASE_URL`).
