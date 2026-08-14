# app/api

Flat FastAPI routers. Most export `router` and are mounted from `app/main.py`.

## Registration

- Default: `prefix=settings.API_PREFIX` so `/agents` → `/api/agents`.
- Self-prefixed (no extra prefix): `triggers.py`, `chat_sessions.py`, `plaza.py`, `webhooks.py`, `websocket.py`, `pages.public_router`, `okr.py`.
- `files.py` exports `router`, `upload_router`, `enterprise_kb_router`.
- `whatsapp.py` is **not** mounted.

## Dependencies

- Users: `current_user: UserRecord = Depends(get_current_user)` - not `User`.
- Admin: `get_current_admin` / `require_role(...)` (both inherit `must_change_password` gate from `get_current_user`).
- Agents: `await check_agent_access(current_user, agent_id)` - leftover `db` args are ignored.
- Persistence: DAOs. Short CRUD routers bind `bind_crud_connection` in `app.main` (one checkout). Do not attach that dependency to websocket / connector inbound / gateway. Extra `async with connection_ctx():` still joins.
- Many handlers still take unused `db=None` (legacy shim). Keep the arg; do not revive a session.
- `websocket.py` and `files.py` download authenticate via `load_user_from_access_token` (identity + force-change). `gateway.py` uses agent API keys.
- `gogcli.py` is mounted (`/api/agents/{id}/gogcli`); users are `UserRecord`.

## Auth / admin genesis

- `auth.py`: password login returns `must_change_password` on `TokenResponse` / `UserOut` / `IdentityOut`. Open registration hard-codes `is_platform_admin=False` (never first-user elevation).
- `PUT /auth/me/password` uses `get_authenticated_user`; rejects `new_password == old_password`; clears `must_change_password`. Password reset also clears the flag.
- `admin.py`: `POST /companies` requires `name`, `admin_email`, `admin_password` (optional display name). Creates tenant + genesis `org_admin` with `must_change_password=True` via `tenant_provisioning`. Unique email race → 409.
- `tenants.py`: `POST /` is platform-admin only and creates a tenant with its genesis org admin (same service as `POST /admin/companies`). Join still calls `raise_if_password_change_required`. The `allow_self_create_company` flag no longer gates tenant creation.
- Inventory for clients: `docs/admin-apis.md`.

## Catalog / secrets

- `tools.py`: tenant override is platform-admin only; catalog mutations need org/platform admin; mask company/agent secrets; `/test-mcp` uses `is_private_url`.
- `update_agent_tool_config`: only `platform_admin` / `org_admin` may set `allow_network` or proxy fields.

## Hotspots

- Large: `feishu.py`, `okr.py`, `enterprise.py`, `auth.py`, `files.py`, `agentbay_control.py`, `websocket.py`, `agents.py`, `tools.py`, `skills.py`, `wecom.py`, `gateway.py`, `tenants.py`, `teams.py`, `admin.py`.
- Split new domain logic out of those files. Do not expose secrets in response models.
