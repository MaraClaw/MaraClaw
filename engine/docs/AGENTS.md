# docs

Operational and reference docs live here. Keep them source-backed; this is not a place for live secrets, generated runtime data, or speculative architecture notes.

## Topics

| Topic | Location | Notes |
|---|---|---|
| Admin APIs (platform + tenant) | `admin-apis.md` | Full inventory of `platform_admin` / `org_admin` HTTP APIs, specs, and role matrix for `web-a` |
| AgentBay | `agentbay.md` | Env vars, API-key lookup order, diagnostics endpoint and CLI |
| gogcli keyring | `gogcli-keyring-password.md` | Password file path, permissions, rotation and security notes |
| gogcli OAuth | `gogcli-oauth-handoff.md` | Website/backend/OpenClaw handoff and credential snapshot lifecycle |
| OpenClaw image helpers | `../docker/openclaw/AGENTS.md` | Entrypoint/CMD chain, pins, keyring file rule |
| ClawSec skills | `clawsec-openclaw-skills.md` | Vendored AGPL ClawSec OpenClaw protection suite and seed wiring |
| Refactoring packets | `refactoring/` | Top-module cleanup briefs. `psycopg-migration.md` is **historical dual-stack** - live policy is the freeze scripts + root `AGENTS.md`. |

## Update Triggers

- Admin API docs should track `app/api/admin.py`, `tenants.py`, `enterprise.py`, `users.py`, `organization.py`, `tools.py`, `skills.py`, and admin gates in `app/core/security.py` when roles or routes change.
- AgentBay docs should track `check_agentbay_config.py`, `app/api/agentbay_control.py`, and AgentBay env vars in `.env.example`.
- gogcli credential docs should track `app/services/gogcli_runtime.py`, `Dockerfile.openclaw`, and `docker/openclaw/` scripts.
- Refactoring packets should name concrete modules, test blockers, and verification status; retire stale blockers when CI/tests move.

## Rules

- Keep docs tied to real files, commands, env vars, or observed behavior in this repo.
- Update docs when runner behavior changes in `start-from-sourcecode.sh`, `start-from-docker.sh`, `entrypoint.sh`, or OpenClaw Docker scripts.
- Prefer concise operational notes over broad design essays; root `AGENTS.md` carries global commands and hierarchy.
- Refactoring notes should name concrete modules and verification blockers rather than vague cleanup goals.
- Credential docs may describe paths and rotation, but must not include live tokens, OAuth refresh secrets, keyring passwords, or customer data.

## Avoid

- Do not store generated reports, logs, browser captures, local `.docker-data`, or temp artifacts here.
- Do not document an integration as supported unless the linked code path and startup/runtime command still exist.
- Do not copy real-looking values from `.env.example` into prose without marking them as examples and checking exposure risk.
