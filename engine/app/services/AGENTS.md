# app/services

This is the main business layer. It is intentionally mixed: flat service modules plus specialized runtime subpackages.

## Boundaries

- Keep route handlers thin. Reusable domain behavior belongs in services.
- New storage behavior goes under `storage_runtime/`.
- New sandbox backends go under `sandbox/local`, `sandbox/api`, or `sandbox/remote` and are registered through `sandbox/registry.py`. Read `sandbox/AGENTS.md` and `sandbox/local/AGENTS.md` before changing isolation, proxy, or bwrap behavior.
- New trigger behavior goes under `trigger_runtime/`, not in `trigger_daemon.py` unless it is process-loop wiring.
- New LLM provider/protocol work goes under `llm/`, in `base.py`, `types.py`, `registry.py`, `factory.py`, or `providers/`. The `llm/client.py` facade is retired; import from the owning module. Company pool RBAC (who may add keys / assign tenant models) lives in `enterprise_llm.py`; routes stay thin. SuperGrok / X Premium device-code is `grok_oauth.py` (fakeable HTTP) + `grok_subscription.py` (session + encrypted pool upsert + refresh). ChatGPT Plus / Pro / Team device-code is `chatgpt_oauth.py` + `chatgpt_subscription.py` (same handoff; Codex JSON device-auth, not RFC 8628). Engine inference uses `create_llm_client_from_model` (Codex Responses), not `/chat/completions`.
- New document conversion belongs in `document_conversion/`, not in `agent_tools.py`.
- Inbound office-document parsing belongs in `document_parser/` (local `anydoc` / `firecrawl-anydoc`). Do not add format-specific extractors in `files.py` or `agent_tool_exec/document_reading.py`. `text_extractor.py` is gone — do not recreate it. Ordinary text decoding stays local. Do not route CSV/HTML/Markdown generation through anydoc.
- New OKR behavior should use focused `okr_*.py` modules. Dashboard period math is `okr_periods.py` (UTC). HTTP models are `app/schemas/okr.py`. Do not grow `okr_reporting.py`. Gap/outreach still lives in `app/api/okr.py`.
- Provider organization department/people sync and its adapter/coordinator package are retired. Shared `org_members` identity/contact/profile data and agent relationships remain; do not recreate the sync subsystem.
- Org departments belong in `org_departments.py` (multiple departments per end user). Set replacement validates every department before writing, locks via `acl_locks`, and transactionally bumps agent ACL versions for the union of old/new `scope_type=department` grants. Singular assignment delegates to replace-with-one or clear.
- New tool execution handlers belong in `agent_tool_exec/` (`@register`). Seed rows go in `tool_definitions/` (composer `builtin.py`). OpenAI catalog shapes go in `agent_tools_definitions/` (package). Runtime visibility/config in `tool_runtime/`. Keep `agent_tools.py` and `tool_seeder.py` as compatibility/orchestration surfaces. Page-read lives in `agent_tool_exec/web_read.py` (`read_webpage`). Web search/fetch/research/extract are official Linkup skills (`linkup_skill_files/` + `linkup_runtime.py` seeder); engine HTTP is `linkup/` key-ring/proxy — not a function-calling tool.

- `tool_config.py` owns tenant-scoped tool settings (`tenant_settings`) and sensitive-field encryption/decryption/masking helpers. Distinct from `tool_runtime/tool_config.py`, which loads, merges, and caches per-agent runtime config.

## God Files

- `agent_tools.py` is the primary no-more-growth file. Avoid adding unrelated tool families, conversion helpers, dispatch branches, or catalog tables there. Leftover names are `@register`ed in `agent_tool_exec/_agent_tool_exec_leftover.py`; `dispatcher.py` uses the registry, with MCP execution for unregistered names, not duplicate builtin branches.
- Keep provider logic in the focused LLM modules, not a recreated `llm/client.py` facade. `tool_seeder.py`, `agentbay_client.py`, `agent_seeder.py`, `feishu_service.py`, `okr_reporting.py`, and `auth_provider.py` are also large enough to prefer adjacent focused modules for new work.
- Other flat hotspots include `skill_seeder.py`, `resource_discovery.py`, `heartbeat.py`, `agent_context.py`, `workspace_collaboration.py`, `email_service.py`, `template_seeder.py`, `channel_user_service.py`, `sso_service.py`, `dingtalk_stream.py`, and `okr_scheduler.py`; extend them only when the change belongs to that exact domain.
- `chat_persist.py` wraps post-LLM message/session/`last_active_at` writes in one `connection_ctx`. `agent_context_cache.py` is the short-TTL Redis cache for soul/memory/skills; invalidate on those workspace writes only. Sets carry `observed_ver` so a stale fill cannot win after invalidate.
- `im_token_cache.py` shares Feishu/WeCom/DingTalk bearer tokens across workers. Keys hash public id **and** secret. DingTalk OAuth2 vs oapi use different provider prefixes. Drop on channel config rotate and WeCom 401. `channels/dedup.py` `*_shared` helpers add Redis SET NX on top of the process-local store.
- `agent_runtime/` is leftover `__pycache__` only (no `.py`, no importers). Do not add `AGENTS.md` or treat it as live. Same for deleted `group_*` / `heartbeat_runtime` bytecode.

## Guest Lifecycle

Operator policy and limitations: [guest containers](../../docs/guest-containers.md).

- Keep orchestration in `agent_lifecycle.py`; identity, admission, ports and observed diagnostics belong in `guest_identity.py`, `guest_admission.py`, `guest_ports.py` and `guest_container_status.py`, not new branches in `agent_manager.py`.
- Canonical names use the full agent UUID. Stop/remove only inspected container IDs with an exact `maraclaw.agent_id` ownership label; legacy short names are eligible only with that label. A conflicting recorded/canonical reference must fail before destructive work.
- Prepare workspace/model/auth/config inputs before destroying an owned guest. Those filesystem/config writes are not transactional and can already affect the existing guest; do not promise atomic replacement or rollback.
- Keep manager-result failures durable: failed start maps to 502 after persisting its error outcome; failed stop records `stopped` for the existing worker retry before 502. Persist and commit these outcomes inside the per-agent flock, before returning the HTTP failure. Preserve the explicit no-Docker `idle` compatibility path.
- Lock order is per-agent flock, then optional shared agents-root `_guest_admission` flock through admission and creation. Aggregate admission coordinates only cooperating engine starts on one host with the shared filesystem; it does not cover Docker restarts, manual Docker or multiple hosts.
- Per-guest limits apply only on new/replacement creation, never automatic live updates. Port allocation prefers the persisted in-range port and permits at most eight total attempts, retrying only recognized bind conflicts.
- `agent_stops.py` must read fresh stopped state under the per-agent lock and use compare-and-set for missing references. Preserve `agent_orphans.py` label/network/age checks and fresh locked ownership/upgrade guards for existing-agent duplicates. Neither path is a worker auto-restart feature.
- Diagnostics are additive, safe observations of Docker health/OOM/limits/ownership/port drift, not readiness guarantees or automatic repair.

## Startup And Seeders

- Seeders are startup-path code and must be idempotent. `app.runtime.lifecycle.lifespan` can invoke `app.runtime.bootstrap.bootstrap` repeatedly when bootstrap is enabled; see `../runtime/AGENTS.md` for ownership.
- Keep optional seed/bootstrap failures scoped and logged; do not make optional seeders bring down unrelated roles unless the caller explicitly requires that.
- **Exception - genesis platform admin:** `platform_admin_seeder.ensure_platform_admin()` is **required** on bootstrap. It first requires usable genesis credentials in the database; only then falls back to `PLATFORM_ADMIN_EMAIL` / `PLATFORM_ADMIN_PASSWORD`. Raises `PlatformAdminSeedError` when neither source is present. `app.runtime.bootstrap` propagates every PA seed failure (fail-closed). Do not demote this to warn-only.
- Platform admin rules: create path sets `must_change_password=True`; existing identity only elevates if env password verifies (never elevate by email alone; never re-enable a disabled identity; never overwrite password when genesis credentials already exist); genesis membership is the **MaraClaw** system org.
- Agent seeders (`agent_seeder`) look up `first_by_role("platform_admin")` and run **after** platform admin seed.
- New companies: `tenant_provisioning.create_tenant_with_org_admin` (slug + identity + `org_admin` + participant + `bind_org_member`). Used by `POST /api/tenants/` and `POST /api/admin/companies`. Duplicate admin email → `AdminEmailTakenError`. The genesis admin email host is claimed as the tenant's default email domain (`techadmin@marathon.vn` → `marathon.vn`). Domain already claimed → `DomainClaimedError`.
- Disable/enable a company: `tenant_lifecycle.set_tenant_active`. MaraClaw/OpenClaw (`is_system` / `is_default_end_user_org`) cannot be disabled. Disable deactivates members (not `platform_admin`), stops agents, and turns off triggers/schedules. Enable restores members only (agents/automations stay stopped).
- Reset/verify email links: `frontend_origin.resolve_frontend_base_url`.
- Additional admins: `admin_provisioning.py`. Genesis is `users.is_genesis`. Only those callers may create another of the same role, or activate/deactivate the other admins of that role.
- Admin trail: `admin_audit.py` → `admin_audit_logs` (actor, action, time, `changes` before/after). Distinct from agent-scoped `audit_logs`. Never raise on write failure.
- New default tools/templates/agents should preserve tenant/global visibility assumptions already encoded in the current seeders.

## Connectors

- Long-running connector runtimes use manager singletons such as `feishu_ws_manager`, `dingtalk_stream_manager`, `wecom_stream_manager`, `wechat_poll_manager`, and `discord_gateway_manager`.
- Connector managers start only under the `connector` role, from lifespan `start_all` **after** `init_pool`. Do not start process-wide loops from ordinary request handlers (one `start_client` after config save is the existing exception).
- Preserve provider-specific identity semantics such as Feishu `user_id` vs `open_id`, WeCom `external_id` vs `unionid`, and SSO secret differences.
- Slack and Teams are webhook/API integrations, not lifespan daemon managers.
- Auth providers, SSO, Google OAuth and IM channels remain supported independently of retired provider directory sync. Do not remove login, identity binding or channel behavior when changing shared organization members.

## Bundled Skill Files

- `skill_creator_files/`, `gogcli_skill_files/`, and `clawsec_skill_files/` are bundled payload directories, not backend service modules.
- `gogcli_skill_files/` is read by `gogcli_runtime.py`; `gogcli_skill_folder_names()` and `seed_gogcli_skill()` only expose/seed these skills when `GOGCLI_ENABLED` is active.
- Each `gogcli_skill_files/<tool>/SKILL.md` frontmatter drives the seeded skill name/description. Do not rename subdirectories or rewrite generated skill text without updating the gogcli packaging/serving path.
- `clawsec_skill_files/` is the vendored ClawSec OpenClaw security suite (AGPL-3.0). It is multi-file (scripts/hooks/advisories) and seeded by `clawsec_runtime.seed_clawsec_skills()` when `CLAWSEC_SKILLS_ENABLED` is active (default true). See `clawsec_skill_files/AGENTS.md` and `NOTICE`.
- Do not apply cosmetic Ruff or type-cleaning to bundled payload content just to satisfy backend checks.
