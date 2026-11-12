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
| `lint.sh` / `format.sh` / `test.sh` | Wrappers; none run the freeze scripts. `format.sh` mutates (`ruff check --fix`, then `ruff format`). |

## Workflows

- `lint.sh` runs Ruff check, `ruff format --check`, `basedpyright --project pyrightconfig.json` and `ty check --force-exclude .`. It does not check contract freshness; CI's `typecheck` job also runs `python -m scripts.export_api_contracts --check`. Run the two `check_no_*.py` scripts yourself when touching imports or logging; CI does not.
- `test.sh` selects no tests: it runs the whole `uv run --extra dev pytest` suite and measures branch coverage for exactly ten modules (`app.api.admin`, `app.api.users`, `app.api.tenants`, `app.services.admin_provisioning`, `app.services.admin_audit`, `app.services.tenant_provisioning`, `app.services.tenant_lifecycle`, `app.services.platform_admin_seeder`, `app.dao.admin_audit_dao`, `app.core.security`) with `--cov-fail-under=90`. It forwards no positional arguments, so run a single test with `uv run pytest <path>` instead. CI sets `PYTEST_ADDOPTS` for XML/HTML reports.
- Contract refresh: finish concurrent engine schema work, run the exporter without `--check`, then run `npm run contracts:generate` in `web-a` and `web-e`. Frontend CI runs `npm run contracts:check`. Details: `../contracts/README.md`.
- The WhatsApp bridge is not built from here: its `npm ci` / `test` / `build` live in `../connectors/whatsapp/`.

## Avoid

- Do not hand-edit `../contracts/api-contract.json`; regenerate it. `--check` compares bytes, so any drift fails.
- A new audited HTTP owner module, enum or public schema name belongs in `api_contract_registry.py`; without it the operation stays in the artifact but not in generated validators.
- Keep `contract_codegen/` dependency-free and free of application imports.
- Do not populate `sqlalchemy_import_allowlist.txt`, including via `check_no_new_sqlalchemy.py --write-allowlist`.
- Scripts are in the basedpyright project (`include` has `scripts`) and may `print` (Ruff `T201` is ignored here); keep them typed.

The baseline is fresh-only and rerunnable, not an upgrade mechanism. Provider organization-sync retirement removes `org_departments` and `org_members.department_id`, `department_path`, `synced_at` from new schemas only. It does not clean existing objects or data; no upgrade patches are provided.
