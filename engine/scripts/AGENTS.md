# scripts

Repo-root operators. Not the same as `app/scripts/` (Python modules).

| File | Role |
|---|---|
| `schema_baseline.sql` | Greenfield DDL (enums, tables, deferred `llm_models` FK) |
| `check_no_new_sqlalchemy.py` | Fail if `app/**/*.py` contains `sqlalchemy`. Empty allowlist. `app/db/` forbidden. |
| `check_no_direct_loguru.py` | Fail if app modules import loguru. Allow `skill_creator_files/`. |
| `lint.sh` / `format.sh` / `test.sh` | Ruff/ty/pytest wrappers — **do not** run the freeze scripts |
| `sqlalchemy_import_allowlist.txt` | Must stay empty. Header comment mentioning `orm_models/` is stale — that tree is gone. |

CI runs both freeze scripts and `ty check .`. Local `lint.sh` uses `ty check --force-exclude` and skips freezes — run the two `check_no_*.py` scripts before merge if you touch imports or logging.
