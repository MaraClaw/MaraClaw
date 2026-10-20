# app/db

Live psycopg3 data-access layer. SQLAlchemy is gone from `app/`.

## Status

- This package **is** the runtime path. There is no `app/database.py` shim.
- Do **not** import SQLAlchemy here (`check_no_new_sqlalchemy.py` forbids `app/db/`).

## Layout

| Module | Role |
|---|---|
| `url.py` | Accept native `postgresql://` / `postgres://` URLs or libpq keyword conninfo; reject driver schemes and the `ssl` query alias (use `sslmode`) |
| `pool.py` | `init_pool` / `close_pool` / `ping_pool` |
| `connection.py` | `DbConnection` (`execute`, `fetch*`, maps errors) |
| `session.py` | `connection_ctx` / `get_db` / `bind_crud_connection` / `optional_connection_ctx` / `transaction` / `run_after_commit` |
| `errors.py` | `DbError`, `UniqueViolationError`, `ForeignKeyViolationError`, `CheckViolationError`, `LockNotAvailableError` (`NOWAIT` / `lock_timeout`) |
| `types.py` | `dict_row`, `as_jsonb` |

## Conventions

- Named `%(name)s` params only. Nested `transaction()` joins (no savepoints). Top-level `connection_ctx` flushes deferred `aclver` bumps after commit.
- `run_after_commit(on_commit, on_rollback=None)` queues synchronous callbacks on the owning top-level `connection_ctx` or `transaction(db=...)`. `on_commit` runs after the commit and the deferred flushes (also after `flush_request_transaction`); `on_rollback` runs when the commit is not reached, including on cancellation. Nested joins share the outer queue, raw savepoints are not callback-aware, and with no open scope `on_commit` runs at once. A failing callback is logged and the rest still run.
- `app.runtime.lifecycle.Runtime.start()` opens the pool **before** bootstrap seeders and role-gated workers/connectors. `get_pool()` raises if unset.
- Pool hygiene: `DATABASE_POOL_MAX_IDLE` (600s) / `DATABASE_POOL_MAX_LIFETIME` (1800s) plus a cheap `closed` check on checkout - not `SELECT 1`.
- `get_db()` exists. Short CRUD routers bind `bind_crud_connection` from `app.main` so DAOs share one checkout.
- Do **not** attach `bind_crud_connection` to websocket, connector inbound, gateway, or AgentBay control (LLM / long I/O).
- Uninitialized pool: `bind_crud_connection` is a no-op (ASGI unit tests).

## Tests

- `tests/test_db_url.py`, `test_db_connection.py`, `test_db_session.py` - no live Postgres.
- CI does not apply `schema_baseline.sql`.
