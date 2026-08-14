# app/schemas

Pydantic request/response models live here. This repo currently has a large shared `schemas.py` plus a small number of focused schema modules.

## Pydantic Rules

- Use Pydantic v2 style: `model_config`, not an inner `Config` class.
- Use `Field(default_factory=...)` for mutable defaults.
- Keep output models from exposing encrypted API keys, cookies, provider secrets, password hashes, or plaintext credential fields.

## Placement

- `schemas.py` is already a cross-domain grab bag. Do not add unrelated schemas to it by default.
- Prefer a domain module when adding a coherent new group of schemas, then re-export only if an existing import surface needs it.
- Endpoint-only request models may remain local to the route module when they are not reused.

## Naming

- Keep `Create`, `Update`, and `Out`/`Response` suffixes aligned with nearby models.
- Make tenant/admin-sensitive response names explicit when the same domain has user and admin views.

## Auth / admin contract notes

- `TokenResponse`, `UserOut`, and `IdentityOut` expose `must_change_password` for first-login force-change UX (web-a and other clients).
- Do not put `password_hash` or env bootstrap secrets on any Out model.
- Company create request/response for platform admin lives on `app/api/admin.py` (`POST /admin/companies`) and `app/api/tenants.py` (`POST /tenants`) models (`admin_email` / `admin_password` in, `org_admin_email` + `must_change_password` out). Shared write path is `app/services/tenant_provisioning.py`. Keep `docs/admin-apis.md` aligned when those change.
