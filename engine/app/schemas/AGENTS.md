# app/schemas

Pydantic request/response models live here. `schemas.py` is the large shared grab bag; 38 focused modules sit beside it (40 `.py` files including the empty `__init__.py`, so there are no package-level re-exports). Import from the owning module.

## Pydantic Rules

- Use Pydantic v2 style: `model_config`, not an inner `Config` class.
- Use `Field(default_factory=...)` for mutable defaults.
- Keep output models from exposing encrypted API keys, cookies, provider secrets, password hashes, or plaintext credential fields.

## Placement

- Focused modules group by owner. Domain request/response: `okr`, `department`, `company_admission`, `company_transfer`, `transfer_policy`, `agent_*` (credential, deletion request, lifecycle, ownership transfer, setup MCP), `openclaw_updates`, `schedule_*`, `trigger_execution`, `tenant_email`, `whatsapp_personal`, `admin_audit`, `admin_metrics`. Audience-split projections: `member_auth` (end-user) beside shared auth DTOs, and the `*_response*` families (`activity`, `auth`, `notification`, `channel`, `collaboration_*`, `content_*`, `operator_*`, `organization_*`). Sockets: `chat_wire.py` owns the closed chat event contract. Reuse a nearby module before opening a new one.
- OKR HTTP models live in `okr.py` (imported by `app/api/okr.py`).
- Org department HTTP models live in `department.py`, not `schemas.py`. Provider directory sync is retired; relationship responses retain `department_path` as an empty string, not a provider hierarchy.
- `schemas.py` is already a cross-domain grab bag. Do not add unrelated schemas to it by default.
- Prefer a domain module when adding a coherent new group of schemas, then re-export only if an existing import surface needs it.
- Endpoint-only request models may remain local to the route module when they are not reused.

## Naming

- Keep `Create`, `Update`, and `Out`/`Response` suffixes aligned with nearby models.
- Make tenant/admin-sensitive response names explicit when the same domain has user and admin views.
- Any HTTP model or `chat_wire.py` change alters the checked `contracts/api-contract.json` (full OpenAPI plus contract registry). Refresh it with `uv run python -m scripts.export_api_contracts` from `engine/`; `--check` is the freshness gate.

## Auth / admin contract notes

- `TokenResponse`, `AuthUser`, and `IdentityOut` expose `must_change_password` for first-login force-change UX (web-a and other clients).
- Do not put `password_hash` or env bootstrap secrets on any Out model.
- Company create request/response for platform admin lives on `app/api/admin.py` (`POST /admin/companies`) and `app/api/tenants.py` (`POST /tenants`) models (`admin_email` / `admin_password` in, `org_admin_email` + `must_change_password` out). Shared write path is `app/services/tenant_provisioning.py`. Keep `docs/admin-apis.md` aligned when those change.
