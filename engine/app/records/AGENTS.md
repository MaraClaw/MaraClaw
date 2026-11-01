# app/records

Plain `@dataclass(slots=True)` rows for the psycopg layer. **Not** ORM models.

- Build via `from_row(dict)` from `DbConnection.fetch*`.
- Pydantic DTO exceptions are frozen, non-row shapes: `AgentSetupStatus` / `AgentSetupInputs` (`agent_setup.py`), `ChatAttachment` (`chat_attachment.py`, strict, `extra="forbid"`), `OutboundTarget` / `OutboundChunk` / `OutboundPlan` (`outbound_plan.py`, persisted credential-free provider payloads) and `PersonalRoute` (`whatsapp_personal.py`). Many records also validate literal columns with a module-level `TypeAdapter`; do not add `BaseModel` to a row record just for that.
- `PersonalRoute.external_id()` packs routing fences into `chat_sessions.external_conv_id` (`VARCHAR(200)`) and raises `PersonalRouteError` (`oversize`, `ambiguous_account`) instead of truncating. Earlier prefixed-JSON routes stay decodable.
- Persist only through DAOs. In-memory slot updates are not auto-saved.
- `UserRecord` is a **tenant membership** (may have `tenant_id=None` for genesis platform admin). Login credentials live on `IdentityRecord`.
- `is_platform_admin` is `role == "platform_admin"` **or** loaded `identity.is_platform_admin`.
- `must_change_password` is proxied from the loaded identity (genesis platform/org admins). Bare `user_dao.get()` does not join identity - force-change always appears false; use `get_with_identity` / `load_user_from_access_token`.
- `IdentityRecord` columns include `must_change_password`, defined only in the fresh-only `scripts/schema_baseline.sql`; there is no bootstrap patch or upgrade backfill. Keep DAO join column lists (`identity_dao`, `user_dao._IDENTITY_COLUMNS`) in sync when adding identity fields.
- `OrgDepartmentRecord` is frozen and represents the org department directory.
- Shared `OrgMemberRecord` retains identity/contact/profile fields and relationship references, without provider-sync `department_id`, `department_path` or `synced_at` fields.
- Use `record_factory = staticmethod(Foo.from_row)` if `BaseDAO.create` should apply dataclass defaults. Plain-function factories skip defaults (`UserDAO` `_user_record_from_row`, `SkillDAO` `_skill_from_row`).
- Do not import SQLAlchemy. Do not put secrets on records that get dumped to API responses.
