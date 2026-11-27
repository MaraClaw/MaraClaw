# scripts

Engine-root operators, run from `engine/`. Not the same as `app/scripts/` (importable bootstrap and one-off modules). CI workflow: `../../.github/workflows/engine-ci.yml`.

| File | Role |
|---|---|
| `schema_baseline.sql` | Greenfield DDL (enums, tables, deferred `llm_models` FK). Includes `identities.must_change_password`, shared `org_members`, relationships and org departments; excludes provider departments and member sync columns. |
| `check_no_new_sqlalchemy.py` | Fail if `app/**/*.py` contains `sqlalchemy`. Empty allowlist. `app/db/` forbidden. |
| `check_no_direct_loguru.py` | Fail if app modules import loguru. Allow `skill_creator_files/`. |
| `sqlalchemy_import_allowlist.txt` | Must stay empty. Header comment mentioning `orm_models/` is stale - that tree is gone. |
| `export_api_contracts.py`, `api_contract_registry.py`, `api_contract_document.py` | Offline exporter for `../contracts/api-contract.json`. Run as `python -m scripts.export_api_contracts` (hence `__init__.py`). Imports `app.main` but never enters lifespan; `--check` never writes. The registry owns `AUDITED_HTTP_MODULES`, enums and `PUBLIC_SCHEMA_NAMES`. |
| `contract_codegen/` | Dependency-free Node generator: `generate.mjs` entry plus `compiler.mjs`, `constraints.mjs`, `document.mjs`. Never imports app code. |
| `verify_anydoc.py` | Proves `firecrawl-anydoc` converts `tests/fixtures/anydoc/supported.docx`; the Dockerfile runs it at build time. |
| `pg-throwaway.sh` | Disposable local PostgreSQL 18.6 for opt-in `*_postgres.py` tests and load tests: `start [--watchdog]`, `dsn`, `stop`. Always `--rm` (a bare `docker run postgres` leaks an anonymous volume per run), durability off, refuses to start under 10 GB host free space. |
| `s3_lifecycle.py` | Lifecycle rules that expire noncurrent object versions under `<S3_PREFIX>/` and, within a day, under `<S3_PREFIX>/state-snapshots/`: `python -m scripts.s3_lifecycle` from `engine/` with the engine's settings. `--check` (default) exits 0 when versioning is off or rules cover both prefixes, 1 when not, 2 for unsupported settings or an S3 error; `--apply [--days N]` (default 30) puts rules `maraclaw-agent-noncurrent-versions` and `maraclaw-state-snapshot-noncurrent-versions` (1 day) and keeps every other rule. Why, IAM and limits: `../docs/managed-storage-quota.md` (Scope), `../docs/agent-state-snapshots.md`. |
| `lint.sh` / `format.sh` / `test.sh` | Wrappers; none run the freeze scripts. `format.sh` mutates (`ruff check --fix`, then `ruff format`). |
| `pytest_shard.py` | CI pytest plugin (`-p scripts.pytest_shard`): SHA-256 of each node ID assigns it to one shard; requires `0 <= --test-shard-index < --test-shard-count`. |

## Workflows

- `lint.sh` runs Ruff check, `ruff format --check`, `basedpyright --project pyrightconfig.json`, `basedpyright --project pyrightconfig.tests.json` and `ty check --force-exclude .`. It does not check contract freshness; CI's `typecheck` job also runs `python -m scripts.export_api_contracts --check`. Run the two `check_no_*.py` scripts yourself when touching imports or logging; CI does not.
- `test.sh` selects no tests: it runs the whole `uv run --extra dev pytest` suite and measures branch coverage for exactly ten modules (`app.api.admin`, `app.api.users`, `app.api.tenants`, `app.services.admin_provisioning`, `app.services.admin_audit`, `app.services.tenant_provisioning`, `app.services.tenant_lifecycle`, `app.services.platform_admin_seeder`, `app.dao.admin_audit_dao`, `app.core.security`) with `--cov-fail-under=90`. It forwards no positional arguments, so run a single test with `uv run pytest <path>` instead. CI sets `PYTEST_ADDOPTS` for XML/HTML reports.
- CI's `lifecycle-postgres` partitions full PostgreSQL/Redis pytest discovery across eight shards with `-p scripts.pytest_shard --test-shard-index=<0-7> --test-shard-count=8`. The database-free scoped gate remains in the separate `test` job.
- Contract refresh: finish concurrent engine schema work, run the exporter without `--check`, then run `npm run contracts:generate` in `web-a` and `web-e`. Frontend CI runs `npm run contracts:check`. Details: `../contracts/README.md`.
- The WhatsApp bridge is not built from here: its `npm ci` / `test` / `build` live in `../connectors/whatsapp/`.

## Avoid

- Do not hand-edit `../contracts/api-contract.json`; regenerate it. `--check` compares bytes, so any drift fails.
- A new audited HTTP owner module, enum or public schema name belongs in `api_contract_registry.py`; without it the operation stays in the artifact but not in generated validators.
- Keep `contract_codegen/` dependency-free and free of application imports.
- Do not populate `sqlalchemy_import_allowlist.txt`, including via `check_no_new_sqlalchemy.py --write-allowlist`.
- Scripts are in the strict basedpyright project (`include` has `scripts`) and may `print` (Ruff `T201` is ignored here); keep them typed.

The baseline is fresh-only and rerunnable, not a general upgrade mechanism. It carries eight guarded upgrades (`tasks` lease columns, `organization_admission_requests` named provisioning, `approval_requests.execution_args`, `gateway_messages.reply_binding_id`, `chat_history_changes.recorded_at`, `users.membership_generation`, the `telegram` / `zalo` channel enum labels, and the catalog-matched `gateway_report_deliveries.im_plan` v1-to-v1/v2 check), listed in `../app/scripts/AGENTS.md`. Provider organization-sync retirement removes `org_departments` and `org_members.department_id`, `department_path`, `synced_at` from new schemas only. It does not clean existing objects or data; no other upgrade patches are provided.

Every start reruns the baseline against a live database, so a rerun on a bootstrapped schema must take no table lock that blocks writers (`tests/test_bootstrap_postgres.py` reruns it while another session holds ROW EXCLUSIVE on every table). `CREATE INDEX IF NOT EXISTS`, `CREATE OR REPLACE TRIGGER` and `ALTER TABLE ... ADD CONSTRAINT` lock their table before they find there is nothing to do, and an `EXCEPTION WHEN duplicate_object` handler only releases that lock after it was granted. So declare every index through `pg_temp.baseline_ensure_index($ddl$CREATE [UNIQUE] INDEX IF NOT EXISTS name ...$ddl$)` (skips an existing name) and every trigger through `pg_temp.baseline_ensure_trigger('<table>', '<name>', $ddl$<pg_get_triggerdef text after the name, %s for the table>$ddl$)` (recreates it only when it is missing, disabled or defined differently, never with `DROP TRIGGER`, which needs ACCESS EXCLUSIVE). Guard any other DDL on an existing table with a catalog lookup, as the `llm_models_tenant_id_fkey` block does. Do not add a marker table, runner-side statement filtering, split transactions or `CREATE INDEX CONCURRENTLY`. Locking and retry: `../app/scripts/AGENTS.md`.
