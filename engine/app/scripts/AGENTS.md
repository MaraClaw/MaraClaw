# app/scripts

One-off modules. Run from repo root: `uv run python -m app.scripts.bootstrap_db`.

## Invocation

- Work behind `main()` / `if __name__ == "__main__"`. Import must not start I/O.
- Use `app.db` (`init_pool` / `connection_ctx`), not a private engine.

## Schema

- `bootstrap_db.py` applies `scripts/schema_baseline.sql` then `PATCHES`.
- Re-runs rely on idempotent `IF NOT EXISTS` SQL. Mapped `DbError` usually skips the Duplicate* ignore branch. Other SQL errors are fatal. Missing baseline is a warning, then PATCHES still run.
- This **is** the schema runner. There is no Alembic.
- New tables go in the baseline. Older DBs get additive `ALTER … IF NOT EXISTS` in `PATCHES` only.
- Force-change column: `identities.must_change_password` is in the baseline and in `PATCHES` for upgrades. Keep both in sync when adding identity columns.

## Avoid

- Do not put daemons here; role-gated loops start from `app.main.lifespan`.
- Do not mention `create_all` or model import lists - those paths are gone.
