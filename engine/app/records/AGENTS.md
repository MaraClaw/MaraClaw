# app/records

Plain `@dataclass(slots=True)` rows for the psycopg layer. **Not** ORM models.

- Build via `from_row(dict)` from `DbConnection.fetch*`.
- Persist only through DAOs. In-memory slot updates are not auto-saved.
- `UserRecord` is a **tenant membership**. Login credentials live on `IdentityRecord`. `is_platform_admin` is `role == "platform_admin"` **or** loaded `identity.is_platform_admin`. Bare `user_dao.get()` does not join identity - use `get_with_identity`.
- Use `record_factory = staticmethod(Foo.from_row)` if `BaseDAO.create` should apply dataclass defaults. Lambdas skip defaults (`UserDAO`, `SkillDAO`).
- Do not import SQLAlchemy. Do not put secrets on records that get dumped to API responses.
