# tests

Root-level `test_*.py` (~130 files). No shared `conftest.py`. Shared helper: `agent_tools_catalog_fakes.py`. OpenClaw image tests also use `openclaw_*_fixtures.py` / `openclaw_officecli_smoke_*.py` in this same directory.

Pytest has no `testpaths`, so it also collects `app/scripts/test_cleanup_duplicate_feishu_users.py`. Repo-root `test_sandbox_config.py` is a print script, not a pytest module. OpenClaw `test_pinned_node_26_5_0_exposes_expected_acorn_ast` needs **host** Node `v26.5.0`; CI has no Node service.

## Commands

```bash
uv run pytest
uv run pytest tests/test_auth.py
uv run pytest -k "storage"
uv run pytest -x --tb=short
```

## Conventions

- `asyncio_mode = "auto"` in `pyproject.toml`. Run from repo root.
- Fakes live in the test file (`DummyResult`, `_FakePool`, `SimpleNamespace`).
- New DB tests wrap a fake raw connection and assert SQL / `%(name)s` params. Reset `_conn_ctx` and `reset_pool_for_tests()` when touching the pool.
- HTTP: `httpx.ASGITransport(app=app)` + `AsyncClient` when you need the stack; most tests call handlers directly.
- Clear `app.dependency_overrides` after use. Storage tests use `tmp_path`.

## Coverage

- Strong: auth, A2A/tools dispatch (`test_agent_tools_dispatch_contract.py` is the name freeze), storage/sandbox helpers, logging service, freeze scripts, schema **text** contracts.
- Genesis admin: `test_admin_genesis.py` — platform seeder (create / elevate-with-password / refuse / fail-closed), `must_change_password` gate, same-password reject, company create response mapping, register non-elevation. Tenant+admin write kwargs live in `test_tenant_create.py`. Extend when changing bootstrap or force-change.
- Thin: live Postgres, `bootstrap_db` execution, full lifespan, Redis, connector daemons, AgentBay/browser, PPTX.
- CI has no database service. Prefer focused tests next to the contract you change.
