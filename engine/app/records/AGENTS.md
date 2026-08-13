# app/records

Plain `@dataclass(slots=True)` rows for the psycopg layer. **Not** ORM models.

- Build via `from_row(dict)` from `DbConnection.fetch*`.
- Persist only through DAOs. In-memory slot updates are not auto-saved.
- `UserRecord` is a **tenant membership** (may have `tenant_id=None` for genesis platform admin). Login credentials live on `IdentityRecord`.
- `is_platform_admin` is `role == "platform_admin"` **or** loaded `identity.is_platform_admin`.
- `must_change_password` is proxied from the loaded identity (genesis platform/org admins). Bare `user_dao.get()` does not join identity - force-change always appears false; use `get_with_identity` / `load_user_from_access_token`.
- `IdentityRecord` columns include `must_change_password` (baseline + bootstrap patch). Keep DAO join column lists (`identity_dao`, `user_dao._IDENTITY_COLUMNS`) in sync when adding identity fields.
- Use `record_factory = staticmethod(Foo.from_row)` if `BaseDAO.create` should apply dataclass defaults. Lambdas skip defaults (`UserDAO`, `SkillDAO`).
- Do not import SQLAlchemy. Do not put secrets on records that get dumped to API responses.
