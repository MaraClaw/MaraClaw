# app/dao

Pure-psycopg repositories returning `app.records` dataclasses.

## Rules

- `async with self.session() as db` → `DbConnection` (joins `connection_ctx`).
- Use `get_many(ids)` / `ANY(%(ids)s)` instead of looping `get()`.
- Parameterized SQL only (`%(name)s`). No SQLAlchemy.
- `BaseDAO.create` fills missing columns from the record dataclass **only if** `record_factory` is `Record.from_row` (not a lambda). `None` defaults are skipped so Postgres `DEFAULT now()` can apply.
- Lambdas today: `UserDAO`, `SkillDAO` - do not copy that pattern. `skills.is_builtin` / `is_default` have no SQL default.
- Largest DAOs: `agent_dao.py`, `chat_dao.py`. Keep new queries in the owning DAO.
- New table: baseline SQL → record → DAO singleton → `dao/__init__.py`. If the table FKs to tenants/agents/users **without** `ON DELETE CASCADE`, add a static delete in `tenant_dao.delete_cascade` **before** the parent row.

## Covered

Most product tables (agents, chat, tools, skills, triggers, plaza, workspace, gogcli, `admin_audit_logs`, …). `dao/__init__.py` is the export list. Agent-scoped events stay in `audit_logs`; admin who/what/when/changes go in `admin_audit_logs`.

No dedicated DAO: `okr_alignments`, `tenant_settings`, `agent_user_onboardings`, `daily_token_usage`.

Cascade gaps vs baseline (will 23503 on real data): `skill_files` before `skills`; `agent_templates.created_by`; `enterprise_info.updated_by`.

## Identity joins

- Auth-critical fields on `identities` include `is_platform_admin`, `email_verified`, and `must_change_password`.
- Any `LEFT JOIN identities` that builds `IdentityRecord` must select **all** identity columns used by `IdentityRecord.from_row` (see `_IDENTITY_COLUMNS` in `identity_dao.py` and `user_dao.py`). Dropping `must_change_password` from the join fails the force-change gate open.

## Avoid

- Do not delete `identities` in tenant cascade (global login rows).
- Do not f-string user input into SQL.
