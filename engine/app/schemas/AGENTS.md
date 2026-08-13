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
