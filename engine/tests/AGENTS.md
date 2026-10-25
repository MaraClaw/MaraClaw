# tests

Root-level `test_*.py`, no shared `conftest.py`. Helpers include `agent_tools_catalog_fakes.py`, `openclaw_*_fixtures.py` and `openclaw_officecli_smoke_*.py` in this directory. Prefer focused tests beside the contract being changed.

Run from `engine/`, not the monorepo root. Pytest has no `testpaths`; package-root `test_sandbox_config.py` is a print script, not a pytest module. OpenClaw node-pin tests need host Node `v26.12.1`; CI has no Node service. Guest: `node:26.12.1-bookworm-slim`; sandbox: `node:26.12.1-slim`; `openclaw_officecli_smoke_probe.py` asserts container `v26.12.1`.

Inbound office parse: `test_document_parser.py`, `test_document_parser_routes.py`, binary `fixtures/anydoc/` (supported/malformed/encrypted/resource-limited/scanned). Do not mock anydoc for these cases. Route tests exercise storage-backed `read_document` for enterprise files and 10-50 MiB inputs.

## Commands

```bash
uv run pytest
uv run pytest tests/test_auth.py
uv run pytest -k "storage"
uv run pytest -x --tb=short
bash scripts/test.sh                      # scoped admin/auth/tenant branch gate: 90%
uv run pytest --cov=app --cov-fail-under=0  # optional full-app report, not the scoped gate
```

## Conventions

- `asyncio_mode = "auto"` in `pyproject.toml`.
- Fakes live in the test file (`DummyResult`, `_FakePool`, `SimpleNamespace`).
- New DB tests wrap a fake raw connection and assert SQL / `%(name)s` params. Reset `_conn_ctx` and `reset_pool_for_tests()` when touching the pool.
- HTTP: `httpx.ASGITransport(app=app)` + `AsyncClient` when you need the stack; most tests call handlers directly.
- Clear `app.dependency_overrides` after use. Storage tests use `tmp_path`.

## Test Navigation And Coverage

- **Gate:** branch coverage with `fail_under=90` on the admin/tenant/auth surface (`app.api.admin`, `app.api.users`, `app.api.tenants`, `admin_provisioning`, `admin_audit`, `tenant_provisioning`, `tenant_lifecycle`, `platform_admin_seeder`, `admin_audit_dao`, `app.core.security`). Scope, terminal report, and fail-under are explicit in `./scripts/test.sh`. Bare `uv run pytest` has no implicit coverage. CI adds XML/HTML reports via `PYTEST_ADDOPTS`.
- The 90% threshold applies only to that scoped gate; do not lower it or describe a full-app report as its result.
- Auth/A2A/tools dispatch, storage/sandbox helpers, logging, freeze scripts and schema text contracts have focused modules. `test_agent_tools_dispatch_contract.py` freezes names; `test_tool_seeder_tables.py` freezes catalog name-order.
- Modularization characterization: `test_okr_api.py` (handler-direct settings/lock/sync/owner/KR buckets), `test_feishu_inbound.py` (challenge / dedup / missing channel / group IDs).
- Genesis admin: `test_admin_genesis.py` - platform seeder (create / elevate-with-password / refuse / fail-closed), `must_change_password` gate, same-password reject, company create response mapping, register non-elevation. Tenant+admin write kwargs live in `test_tenant_create.py`. Additional-admin minting (genesis-only) lives in `test_admin_provisioning.py`.
- Runtime: `test_process_roles.py`, `test_runtime_*.py` cover normalized entrypoint decisions, bootstrap ordering, owned lifecycle/cancellation, readiness and proxy cleanup; `test_bootstrap_runner.py` covers schema-runner failures/cleanup. See `../app/runtime/AGENTS.md`.
- Fresh schema: `test_bootstrap_identity_schema.py` checks retired provider-department objects and the retained shared member columns. `test_bootstrap_schema.py` covers the wider DDL contract.
- Live PostgreSQL: `test_bootstrap_postgres.py` opts in with `BOOTSTRAP_TEST_DATABASE_URL`, creates a unique schema and drops it afterward. It asserts provider-sync table/column removal, retained member/relationship FKs and org department constraints. These tests skip when unset; fakes/text contracts do not prove live integration.
- OpenClaw upgrades: `test_openclaw_update_executor.py` (fakes in `openclaw_update_fakes.py`: success, pull/start/readiness/publish failures, interrupted cutover, deleted/disabled agent), `_snapshot`, `_gates`, `_exclusion`, `_api`; `test_openclaw_update_postgres.py` (opt-in `BOOTSTRAP_TEST_DATABASE_URL`: fan-out matrix, replay, outstanding/gate semantics, publication CAS). Tests that call the gated seams or file routes patch the gate via `openclaw_update_gate_fixtures.py` or `assert_work_allowed`.
- `../../.github/workflows/engine-ci.yml` keeps the scoped gate database-free and separately runs `lifecycle-postgres` against PostgreSQL 18.6: setup, setup retry/authorization, start recovery, deletion requests/cleanup/approval, ownership transfers and guest-config transactions. Fixture DSNs are `AGENT_SETUP_TEST_DATABASE_URL` (also requires `AGENT_SETUP_TEST_DATABASE_DISPOSABLE=1`), `MARACLAW_TEST_POSTGRES_DSN`, `CHAT_SUBMISSION_TEST_DATABASE_URL` and `BOOTSTRAP_TEST_DATABASE_URL` (start tests reuse the upgrade fixture). Each live fixture isolates and drops its schema. External Redis/connectors/AgentBay/browser/image smoke need their matching integration environment; do not infer live coverage from isolated tests.
