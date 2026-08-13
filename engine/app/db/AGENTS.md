# app/db

Live psycopg3 data-access layer. SQLAlchemy is gone from `app/`.

## Status

- This package **is** the runtime path. `app/database.py`: `async_session` raises; `transaction()` forwards here; `transaction(session=obj)` yields `obj` unchanged (tests).
- Do **not** import SQLAlchemy here (`check_no_new_sqlalchemy.py` forbids `app/db/`).

## Layout

| Module | Role |
|---|---|
| `url.py` | Normalize `DATABASE_URL` (`+asyncpg`/`+psycopg`, `ssl` → `sslmode`) |
| `pool.py` | `init_pool` / `close_pool` / `ping_pool` |
| `connection.py` | `DbConnection` (`execute`, `fetch*`, maps errors) |
| `session.py` | `connection_ctx` / `get_db` / `bind_crud_connection` / `optional_connection_ctx` / `transaction` |
| `errors.py` | `DbError`, `UniqueViolationError`, `ForeignKeyViolationError`, `CheckViolationError` |
| `types.py` | `dict_row`, `as_jsonb` |

## Conventions

- Named `%(name)s` params only. Nested `transaction()` joins (no savepoints). Top-level `connection_ctx` flushes deferred `aclver` bumps after commit.
- Pool opens in `app.main` lifespan **before** seeders/connectors. `get_pool()` raises if unset.
- Pool hygiene: `DATABASE_POOL_MAX_IDLE` (600s) / `DATABASE_POOL_MAX_LIFETIME` (1800s) plus a cheap `closed` check on checkout — not `SELECT 1`.
- `get_db()` exists. Short CRUD routers bind `bind_crud_connection` from `app.main` so DAOs share one checkout.
- Do **not** attach `bind_crud_connection` to websocket, connector inbound, gateway, or AgentBay control (LLM / long I/O).
- Uninitialized pool: `bind_crud_connection` is a no-op (ASGI unit tests).

## Tests

- `tests/test_db_url.py`, `test_db_connection.py`, `test_db_session.py` — no live Postgres.
- CI does not apply `schema_baseline.sql`.
