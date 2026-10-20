# app/scripts

One-off modules. Run from `engine/`: `uv run python -m app.scripts.bootstrap_db`.

## Invocation

- Work behind `main()` / `if __name__ == "__main__"`. Import must not start I/O.
- Use `app.db` (`init_pool` / `connection_ctx`), not a private engine.

## Schema

- `bootstrap_db.py` applies only `scripts/schema_baseline.sql` to fresh databases.
- Re-runs rely on idempotent `IF NOT EXISTS` SQL. Missing baseline and SQL errors are fatal; the pool is closed in `finally`.
- This **is** the schema runner. There is no Alembic.
- All current definitions, including force-change and org departments, live only in the baseline. There is no upgrade path or historical backfill.
- Dirty databases are unsupported; duplicate identity memberships must fail unique-index creation, not skip it.

## Avoid

- Do not put daemons here; role-gated loops start from `app.runtime.lifecycle.Runtime.start()`.
- Do not mention `create_all` or model import lists - those paths are gone.
