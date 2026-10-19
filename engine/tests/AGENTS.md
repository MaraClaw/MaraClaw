# tests

Root-level `test_*.py` (~160 files). No shared `conftest.py`. Shared helper: `agent_tools_catalog_fakes.py`. OpenClaw image tests also use `openclaw_*_fixtures.py` / `openclaw_officecli_smoke_*.py` in this same directory.

Pytest has no `testpaths`. Repo-root `test_sandbox_config.py` is a print script, not a pytest module. OpenClaw node-pin tests need **host** Node `v26.11.1`; this checkout has no CI Node service. Guest image is `node:26.11.1-bookworm-slim`; sandbox docker is `node:26.11.1-slim`. `openclaw_officecli_smoke_probe.py` asserts container `v26.11.1`. Coverage scope, terminal report, and the 90% fail-under gate are explicit in `scripts/test.sh`, not default `addopts`.

Inbound office parse: `test_document_parser.py` + `test_document_parser_routes.py` + binary fixtures in `fixtures/anydoc/` (supported / malformed / encrypted / resource-limited / scanned). Do not mock `anydoc` for those cases. Storage-backed `read_document` (enterprise + 10–50 MiB) is covered in the route file.

## Commands

```bash
uv run pytest
uv run pytest tests/test_auth.py
uv run pytest -k "storage"
uv run pytest -x --tb=short
uv run pytest --cov=app --cov-fail-under=0   # full-app report (~55% measured snapshot)
```

## Conventions

- `asyncio_mode = "auto"` in `pyproject.toml`. Run from repo root.
- Fakes live in the test file (`DummyResult`, `_FakePool`, `SimpleNamespace`).
- New DB tests wrap a fake raw connection and assert SQL / `%(name)s` params. Reset `_conn_ctx` and `reset_pool_for_tests()` when touching the pool.
- HTTP: `httpx.ASGITransport(app=app)` + `AsyncClient` when you need the stack; most tests call handlers directly.
- Clear `app.dependency_overrides` after use. Storage tests use `tmp_path`.

## Coverage

- **Gate:** branch coverage with `fail_under=90` on the admin/tenant/auth surface (`app.api.admin`, `app.api.users`, `app.api.tenants`, `admin_provisioning`, `admin_audit`, `tenant_provisioning`, `tenant_lifecycle`, `platform_admin_seeder`, `admin_audit_dao`, `app.core.security`). Scope, terminal report, and fail-under are explicit in `./scripts/test.sh`. Bare `uv run pytest` has no implicit coverage. CI adds XML/HTML reports via `PYTEST_ADDOPTS`.
- Full `app/` measured snapshot is ~55% (51k statements), separate from the scoped admin/tenant/auth result of 94.46% against the unchanged 90% gate. The 90% threshold applies only to the scoped gate; do not lower it.
- Strong: auth, A2A/tools dispatch (`test_agent_tools_dispatch_contract.py` is the name freeze), catalog name-order (`test_tool_seeder_tables.py`), storage/sandbox helpers, logging service, freeze scripts, schema **text** contracts.
- Modularization characterization: `test_okr_api.py` (handler-direct settings/lock/sync/owner/KR buckets), `test_feishu_inbound.py` (challenge / dedup / missing channel / group IDs).
- Genesis admin: `test_admin_genesis.py` - platform seeder (create / elevate-with-password / refuse / fail-closed), `must_change_password` gate, same-password reject, company create response mapping, register non-elevation. Tenant+admin write kwargs live in `test_tenant_create.py`. Additional-admin minting (genesis-only) lives in `test_admin_provisioning.py`.
- Thin: live Postgres, `bootstrap_db` execution, full lifespan, Redis, connector daemons, AgentBay/browser, PPTX.
- Root `../.github/workflows/engine-ci.yml` runs the scoped coverage gate without a database service. Prefer focused tests next to the contract you change.
