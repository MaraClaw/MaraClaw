# Psycopg migration notes

Program plan: `.omgb/plans/psycopg-db-migration-20260812.md`.

## Phase 0/1 (landed)

- New package: `app/db/` (pure psycopg3 pool + connection + session helpers).
- Dual-stack: SQLAlchemy remains for existing modules; lifespan opens the psycopg pool alongside.
- Freeze: `scripts/check_no_new_sqlalchemy.py` + `scripts/sqlalchemy_import_allowlist.txt` (CI lint job).
- Deps: `psycopg[binary]`, `psycopg-pool` (aligned binary 3.3.4 for Python 3.14).

## Rules for new work

1. Prefer `app.db` for any new database access.
2. Do not add SQLAlchemy imports to files outside the allowlist.
3. Parameterized SQL only (`%(name)s` / `%s`); never f-string user input.
4. Nested `app.db.transaction()` joins the outer connection.

## Phase 2 (landed)

- `app/records/*` plain dataclasses for identity/user/tenant/org/settings/etc.
- `app/dao/*` rewritten on pure psycopg (`BaseDAO` + SQL); no SQLAlchemy imports.
- Call sites updated for explicit `update()` instead of ORM dirty tracking.

## Phase 3a (landed)

- `get_current_user` / `get_authenticated_user` / `get_current_admin` load users via
  `user_dao.get_with_identity` (no SQLAlchemy `get_db` dependency).

## Phase 3b (landed)

- `app/services/sso_service.py` - pure DAO/psycopg (no SQLAlchemy).
- `app/services/auth_provider.py` - `find_or_create_user` + provider ensure/create on DAOs.
- `app/services/identity_provider_lookup.py` - DAO-backed preferred-provider helper.
- Call sites updated across auth/registration/connectors.

## Phase 3c (partial → expanded)

- `AgentRecord` / `AgentPermissionRecord` + `agent_dao` / `agent_permission_dao`
- Access helpers (`check_agent_access`, relationship status, accessible user ids) use DAOs
- `list_agents` + relationship candidate/search paths use `list_visible_agents`
- Lazy token resets + unread counts via agent DAO / pure SQL
- `onboarded_agent_ids` pure-psycopg
- `agent_manager` primary model load via `llm_model_dao` + `LLMModelRecord`
- Chat session ensure/primary + tool-call log via `chat_session_dao` / `chat_message_dao`
- Trigger queue/claim/complete/fail/mark-fired + dispatch enqueue via trigger DAOs
- Audit logger + notification send via pure-psycopg

## Phase 5 (Alembic SQL-only posture - landed)

- `ALEMBIC_GUIDELINES.md` + `alembic/AGENTS.md` require hand-written SQL revisions
- `alembic/env.py` documents no new `--autogenerate`; `Base.metadata` retained for inspection only
- Thin SQLAlchemy engine remains for the Alembic runner until Phase 4

## Phase 3 seeders + tools catalog (landed)

- Skill/tool/template seeders, ClawSec/gogcli skill seed via DAOs
- LLM tool catalog assembly + tenant tool config pure-psycopg
- Agent template list endpoint via `agent_template_dao`

## Phase 3 chat/websocket + org_sync paths (landed)

- `chat_sessions` API, channel session find-or-create, websocket setup/save paths
- Onboarding resolve/mark pure-psycopg
- Org sync department reconcile/upsert/path rebuild pure-psycopg

## Phase 3 org members + channel user (landed)

- Org sync member upsert/lookup + adapter factory pure-psycopg
- Channel user resolution and platform-user-from-org-member pure-psycopg

## Phase 3 connectors (landed)

- Feishu login_or_register + shared `_load_agent_and_model` pure-psycopg
- WeChat message processor / channel poller / context cache pure-psycopg
- WeCom stream message pipeline + start_all; DingTalk start_all pure-psycopg
- `channel_config_dao` / `ChannelConfigRecord`

## Residual

Still ~120 `app/**` files import SQLAlchemy (remaining API modules, LLM caller,
ORM models). **Phase 4 full runtime removal is blocked** until those domain
waves finish.

## Next

- Remaining high-traffic APIs
- Shrink freeze allowlist further
- Phase 4: drop dual-stack engine when allowlist is empty
