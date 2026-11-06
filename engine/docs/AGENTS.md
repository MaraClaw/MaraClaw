# docs

Operational and reference docs live here. Keep them source-backed; this is not a place for live secrets, generated runtime data, or speculative architecture notes.

## Topics

| Topic | Location | Notes |
|---|---|---|
| Admin APIs (platform + tenant) | `admin-apis.md` | Full inventory of `platform_admin` / `org_admin` HTTP APIs, genesis bootstrap, `must_change_password`, role matrix for `web-a` |
| Channels | `channels.md` | Transport/maturity per IM product. **Cloud API WhatsApp**: signed webhook, per-message admission, 24-hour reply window from the signed inbound timestamp. **Personal QR WhatsApp**: self-chat only, durable burst collection, `WHATSAPP_PERSONAL_BURST_*` limits. Bridge process contract is not here: `../connectors/whatsapp/AGENTS.md` |
| Shared human runtime | `shared-openclaw-runtime.md` | One shared human transcript lane per agent, FIFO head dispatch, destination contracts, guest session identity, focused verification |
| Gateway reports | `gateway-report-delivery.md` | Native terminal settlement and independent `/gateway/report`, durable agent and IM destinations, chunk checkpoints, provider backoff, delivery-ambiguity limits |
| Guest containers | `guest-containers.md` | Engine-side ownership/replacement, readiness deadlines, bootstrap handshake, admission limits; policy, not live-host validation |
| AgentBay | `agentbay.md` | Env vars, API-key lookup order, diagnostics endpoint and CLI |
| gogcli keyring | `gogcli-keyring-password.md` | Password file path, permissions, rotation and security notes |
| gogcli OAuth | `gogcli-oauth-handoff.md` | Website/backend/OpenClaw handoff and credential snapshot lifecycle |
| OpenClaw image helpers | Guest image project `docker/openclaw/AGENTS.md` | Entrypoint/CMD chain, pins, keyring file rule; the guest image recipe, build and publish live in the sibling guest image project, outside this monorepo |
| OpenClaw upgrades | `openclaw-updates.md` | Global notices, per-agent worker cutover/rollback, work and write gates, HTTP contract |
| Managed storage quota | `managed-storage-quota.md` | Per-agent S3 byte ledger: scope and exclusive prefix ownership, reservation/settle protocol, ambiguous outcomes, tree delete, transfer preflight, 413/507/503 contract, open retirement decision |
| Web search analytics | `web-search-analytics.md` | `web_search_events` capture/export; never log raw `q` / URL / body |
| Product contracts | `chat-attachments.md`, `chat-history.md`, `company-admissions.md`, `broadcast-email-delivery.md`, `member-timezone.md`, `system-alerts.md` | One feature each; read the matching file before changing that API or table |
| Refactoring packets | `refactoring/` | July 2026 briefs (stale metrics). Org-sync references are historical: provider directory sync is retired. `psycopg-migration.md` is **historical dual-stack**. |

Offline API contracts are documented in `../contracts/README.md`, not here.

## Update Triggers

- Admin API docs should track `app/api/admin.py`, `tenants.py`, `enterprise.py`, `users.py`, `organization.py`, `tools.py`, `skills.py`, `auth.py` (login/password force-change), `app/services/platform_admin_seeder.py`, `app/services/tenant_provisioning.py`, and admin gates in `app/core/security.py` when roles, genesis bootstrap, or routes change.
- Channel docs should track `app/api/whatsapp.py`, `app/api/whatsapp_personal.py`, `app/services/channels/` (`whatsapp_window.py`, `whatsapp_personal_*.py`, `personal_burst_*.py`) and the `WHATSAPP_PERSONAL_BURST_*` settings.
- Shared-runtime and gateway docs should track `app/services/openclaw_shared_dispatch.py`, `openclaw_native_claims.py`, `openclaw_routing.py`, `openclaw_gateway_rpc.py`, `openclaw_gateway_delivery.py`, `openclaw_gateway_config.py`, `openclaw_gateway_stream.py`, `response_identity.py`, `openclaw_dispatch_reconciler.py`, `shared_chat.py`, `gateway_report_relay.py`, `connectors/openclaw-dispatcher/gateway-rpc.mjs` and `app/api/gateway.py`.
- Guest-container docs should track `app/services/guest_*.py` and `agent_start_*.py`.
- AgentBay docs should track `check_agentbay_config.py`, `app/api/agentbay_control.py`, and AgentBay env vars in `.env.example`.
- OpenClaw upgrade docs should track `app/api/openclaw_updates.py`, `app/services/openclaw_update_*.py`, `app/dao/openclaw_update_dao.py` and the work seams in `openclaw_routing.py` / `openclaw_gateway_delivery.py`.
- Managed storage quota docs should track `app/services/storage_runtime/quota_*.py`, `facade.py`, `app/dao/agent_storage_dao.py`, `app/db/pool.py` (`durable_pool`), `app/services/company_transfer_disposition.py` and the `agent_storage_*` tables.
- Search-analytics docs should track `app/services/linkup/`, `app/api/admin_search_analytics.py`, `app/api/linkup_proxy.py`, and `WEB_SEARCH_ANALYTICS_*` env vars.
- gogcli credential docs should track `app/services/gogcli_runtime.py` and the guest image project's `Dockerfile.openclaw` and `docker/openclaw/` scripts.
- Refactoring packets should name concrete modules, test blockers, and verification status; retire stale blockers when CI/tests move.

## Rules

- Keep docs tied to real files, commands, env vars, or observed behavior in this repo.
- Update docs when runner behavior changes in `start-from-docker.sh`, `entrypoint.sh`, or OpenClaw Docker scripts.
- Prefer concise operational notes over broad design essays; `../AGENTS.md` carries engine commands and hierarchy.
- Credential docs may describe paths and rotation, but must not include live tokens, OAuth refresh secrets, keyring passwords, or customer data.
- Schema-affecting docs must say whether a change is fresh-baseline only; no existing-database upgrade is supplied.

## Avoid

- Do not store generated reports, logs, browser captures, local `.docker-data`, or temp artifacts here.
- Do not document an integration as supported unless the linked code path and startup/runtime command still exist.
- Do not copy real-looking values from `.env.example` into prose without marking them as examples and checking exposure risk.
