# scripts

Repo-root operators. Not the same as `app/scripts/` (Python modules).

| File | Role |
|---|---|
| `schema_baseline.sql` | Greenfield DDL (enums, tables, deferred `llm_models` FK). Includes `identities.must_change_password`. |
| `check_no_new_sqlalchemy.py` | Fail if `app/**/*.py` contains `sqlalchemy`. Empty allowlist. `app/db/` forbidden. |
| `check_no_direct_loguru.py` | Fail if app modules import loguru. Allow `skill_creator_files/`. |
| `lint.sh` / `format.sh` / `test.sh` | Ruff/ty/pytest wrappers - **do not** run the freeze scripts |
| `sqlalchemy_import_allowlist.txt` | Must stay empty. Header comment mentioning `orm_models/` is stale - that tree is gone. |

Root `../.github/workflows/engine-ci.yml` runs `test.sh` and adds XML/HTML reports via `PYTEST_ADDOPTS`. Local `lint.sh` uses `ty check --force-exclude` and skips freezes - run the two `check_no_*.py` scripts before merge if you touch imports or logging. `test.sh` explicitly selects the ten admin/auth/tenant modules, terminal report, and 90% branch-coverage gate; bare pytest has no implicit coverage. The wrapper does not pass through positional arguments.
