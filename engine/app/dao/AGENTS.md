# app/dao

Pure-psycopg repositories returning `app.records` dataclasses.

## Rules

- `async with self.session() as db` → `DbConnection` (joins `connection_ctx`).
- Use `get_many(ids)` / `ANY(%(ids)s)` instead of looping `get()`.
- Parameterized SQL only (`%(name)s`). No SQLAlchemy.
- `BaseDAO.create` fills missing columns from the record dataclass **only if** `record_factory` is `Record.from_row` (not a lambda). `None` defaults are skipped so Postgres `DEFAULT now()` can apply.
- Lambdas today: `UserDAO`, `SkillDAO` - do not copy that pattern. `skills.is_builtin` / `is_default` have no SQL default.
- Largest DAOs: `agent_dao.py`, `chat_dao.py`. Keep new queries in the owning DAO.
- Tenant tier: `tenants.tier` is `NOT NULL` with no default, so every `tenant_dao.create` passes it. `tenant_dao.tiers_for(ids)` reads it for many organizations in one query inside the caller's transaction (never the tenant cache); `organization_plans.tier_for_tenant` is its single-id form.
- Clawbook @mentions: `agent_dao.find_by_mention_names` and `user_dao.find_active_by_mention_names` take the distinct lowercased tokens and run one query each, whatever the roster size. `mention_patterns.like_patterns` builds `ILIKE ANY` patterns that return a superset under C, libc, builtin and ICU collations, including the Turkish and Lithuanian tailorings that change a lowered name's length, and Python's `name.lower() in tokens` stays the exact rule; never decide a match with SQL `lower()`. Same-named records resolve to the oldest, then the lowest id. Mention paths never call `list_for_tenant`, `list_active_for_tenant` or `get_all`.
- Tenant name search: `tenant_dao.search_by_name` uses generated `name_tsv` + prefix `to_tsquery('simple', 'mara:*')`. Tokens must stay `[a-z0-9]` (see `tenant_name_tsquery`).
- Partial JSONB state (e.g. trigger `_last_value` / `_poll_seq`) uses a conditional `config || jsonb_build_object(...)` UPDATE with a `WHERE` on the expected value, not `BaseDAO.update` (whole-row replace loses concurrent writes).
- Keyset paging over nullable sort keys needs an explicit NULL branch in the cursor predicate (`chat_dao.list_page`); never OFFSET.
- New table: baseline SQL → record → DAO singleton → `dao/__init__.py`. If the table FKs to tenants/agents/users **without** `ON DELETE CASCADE`, add a static delete in `tenant_dao.delete_cascade` **before** the parent row.

## Row typing (strict)

- Rows are `Mapping[str, object]` (what `BaseDAO` hands `record_factory`); never `dict[str, Any]` or psycopg `class_row`. `from_row` narrows each cell with an `app.core.json_types` helper (`str_from_row`, `int_from_row`, `uuid_from_row`, `datetime_from_row`, ...).
- JSONB object columns are `JsonObject` fields filled by `json_object_from(...)` (deep check; a non-object becomes `{}`). Import `JsonObject`/`JsonValue` at runtime: `default_factory=dict[str, JsonValue]` evaluates it.
- `mapping_from_row` / `is_str_dict` / `is_any_list` / `object_list_from_row` narrow an untyped `object` to `dict[str, object]` / `list[object]`. On a value already typed `JsonValue` use `isinstance(x, list)` or `is_json_object(x)`: ty treats `list[JsonValue]` and `list[object]` as disjoint.
- `dict` is invariant: build records from variables annotated `JsonObject`, not `dict[str, str]` / `dict[str, object]`. `JsonObject.get()` is `JsonValue | None`: test it with `(v := ...) is not None and v` or narrow with `json_as_str` and friends.

```python
# before
config: dict[str, Any] = field(default_factory=dict[str, Any])
config = mapping_from_row(row.get("config") or {})
# after
config: JsonObject = field(default_factory=dict[str, JsonValue])
config = json_object_from(row.get("config") or {})
```

## Covered

Most product tables (agents, chat, tools, skills, triggers, clawbook, workspace, gogcli, `admin_audit_logs`, `web_search_events`, …). `dao/__init__.py` is the export list. Agent-scoped events stay in `audit_logs`; admin who/what/when/changes go in `admin_audit_logs`. `web_search_events` has **no FK** to tenants/agents — do not add it to `delete_cascade`.

`org_department_dao` owns the persisted tables `local_departments` and `local_department_memberships` (PK `user_id, department_id`, multiple memberships). Replacement deletes and inserts the normalized set in one transaction; assignment listings retain one row per membership. Provider organization sync and its department storage are retired. `has_agent_access` matches only `agents.access_mode = custom` and `agent_permissions.access_level = use`.

No dedicated DAO: `okr_alignments`, `tenant_settings`, `agent_user_onboardings`, `daily_token_usage`.

Cascade gaps vs baseline (will 23503 on real data): `skill_files` before `skills`; `agent_templates.created_by`; `enterprise_info.updated_by`.

## Identity joins

- Auth-critical fields on `identities` include `is_platform_admin`, `email_verified`, and `must_change_password`.
- Any `LEFT JOIN identities` that builds `IdentityRecord` must select **all** identity columns used by `IdentityRecord.from_row` (see `_IDENTITY_COLUMNS` in `identity_dao.py` and `user_dao.py`). Dropping `must_change_password` from the join fails the force-change gate open.
- `UserDAO.get_with_identity` may serve a Redis session snapshot (no `password_hash`, no quota counters). Updates / tenant deactivate / `delete_cascade` must bump session/tenant versions. Password verify uses SQL via `load_identity_for_password`. Redis errors fall back to this join.
- `users.membership_generation` is deliberately not in `_USER_COLUMNS` or `UserRecord` (a snapshot would decode a missing field as 0): read it with `UserDAO.get_membership_generation` (SQL), or together with the identity's `auth_generation` in one statement with `UserDAO.get_generations(user_id, identity_id)`, which is what every bearer check uses. Every active-to-inactive writer advances it in its own UPDATE: `deactivate_for_tenant` and `deactivate_unless_last_active` add `+ 1` next to `is_active = FALSE` (both already filter `is_active IS TRUE`), and `UserDAO.update` gets a guarded `CASE WHEN is_active AND NOT %(is_active)s` expression through `BaseDAO.update_side_effects`, so one statement both writes and advances, and activation or a repeated deactivation never advances. Do not bump it in a second statement and do not touch `identities.auth_generation` here.

## Avoid

- Do not delete `identities` in tenant cascade (global login rows).
- Do not f-string user input into SQL.
