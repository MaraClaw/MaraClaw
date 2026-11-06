# app/scripts

One-off modules. Run from `engine/`: `uv run python -m app.scripts.bootstrap_db`.

## Invocation

- Work behind `main()` / `if __name__ == "__main__"`. Import must not start I/O.
- Use `app.db` (`init_pool` / `connection_ctx`), not a private engine.

## Schema

- `bootstrap_db.py` applies only `scripts/schema_baseline.sql`: fresh databases, plus the three explicit legacy upgrades below.
- Re-runs rely on idempotent `IF NOT EXISTS` SQL. Missing baseline and SQL errors are fatal; the pool is closed in `finally`.
- This **is** the schema runner. There is no Alembic.
- All current definitions, including force-change and org departments, live only in the baseline. The only upgrade is the guarded `DO` block after `CREATE TABLE tasks` that adds `claim_token`, `lease_expires_at` and `tasks_check` to pre-lease tables (rows kept, unleased; rerun skips). The second is the guarded `DO` block after `CREATE TABLE organization_admission_requests`: when `organization_name` is missing it adds that column plus `provisioned_at`, `provisioned_tenant_id`, `provisioned_admin_user_id` and the named checks `ck_organization_admission_name` / `ck_organization_admission_provisioned` in one `ALTER` (rows kept with a NULL name and never provisioned; rerun skips; `tests/test_bootstrap_postgres.py` proves it matches the fresh definition). The third is the guarded `DO` block after `CREATE TABLE approval_requests`: when `execution_args` is missing it adds that nullable `JSONB` column (the private full tool-argument object replayed after approval; rows kept with NULL, which keeps the old `details`-based replay; rerun skips; the same test module proves it matches the fresh definition). No other upgrade path or historical backfill; add none without a blocked bootstrap and a preserved-rows PostgreSQL test.
- Dirty databases are unsupported; duplicate identity memberships must fail unique-index creation, not skip it.

## Avoid

- Do not put daemons here; role-gated loops start from `app.runtime.lifecycle.Runtime.start()`.
- Do not mention `create_all` or model import lists - those paths are gone.
