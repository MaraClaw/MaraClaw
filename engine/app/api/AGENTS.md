# app/api

Flat FastAPI routers. Most export `router` and are mounted from `app/main.py`.

## Registration

- Default: `prefix=settings.API_PREFIX` so `/agents` → `/api/agents`.
- Self-prefixed (no extra prefix): `triggers.py`, `chat_sessions.py`, `plaza.py`, `webhooks.py`, `websocket.py`, `pages.public_router`, `okr.py`.
- `files.py` exports `router`, `upload_router`, `enterprise_kb_router`.
- `whatsapp.py` is **not** mounted.

## Dependencies

- Users: `current_user: UserRecord = Depends(get_current_user)` - not `User`.
- Admin: `get_current_admin` / `require_role(...)`.
- Agents: `await check_agent_access(current_user, agent_id)` - leftover `db` args are ignored.
- Persistence: DAOs. Short CRUD routers bind `bind_crud_connection` in `app.main` (one checkout). Do not attach that dependency to websocket / connector inbound / gateway. Extra `async with connection_ctx():` still joins.
- Many handlers still take unused `db=None` (legacy shim). Keep the arg; do not revive a session.
- `websocket.py` authenticates from query token. `gateway.py` uses agent API keys.
- `gogcli.py` is mounted (`/api/agents/{id}/gogcli`); users are `UserRecord`.

## Catalog / secrets

- `tools.py`: tenant override is platform-admin only; catalog mutations need org/platform admin; mask company/agent secrets; `/test-mcp` uses `is_private_url`.
- `update_agent_tool_config`: only `platform_admin` / `org_admin` may set `allow_network` or proxy fields.

## Hotspots

- Large: `feishu.py`, `okr.py`, `enterprise.py`, `auth.py`, `files.py`, `agentbay_control.py`, `websocket.py`, `agents.py`, `tools.py`, `skills.py`, `wecom.py`, `gateway.py`, `tenants.py`, `teams.py`.
- Split new domain logic out of those files. Do not expose secrets in response models.
