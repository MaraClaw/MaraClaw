# app/core

`app/core` contains cross-cutting primitives only: tracing, logging setup, JWT/auth dependencies, permission policy, Redis Pub/Sub lifecycle, and low-level email transport helpers.

## Trace And Logging

- `TraceIdMiddleware` reads or creates `X-Trace-Id`, stores `request.state.trace_id`, adds the response header, and logs request/response lines.
- `app.main` installs trace middleware first. Preserve that order for request-wide tracing.
- Starlette wraps middleware in reverse order; verify runtime behavior before relying on trace logs for CORS/error paths.
- Use `from app.core.logging import logger`. `intercept_standard_logging()` redirects stdlib logging into the process logger at startup.
- Background jobs that are not inside an HTTP request should call `new_trace_id()` before emitting related logs.
- WebSocket flows set trace ids manually because they do not follow the normal HTTP request lifecycle.

## Security

- `security.py` owns JWT creation/decoding, password hashing, and FastAPI auth dependencies.
- Shared loader: `load_user_from_access_token(token, require_active=..., enforce_password_change=...)` loads `UserRecord` **with identity** and optionally enforces `must_change_password`.
- Use from route modules:
  - `get_current_user` — active user; **403** with `{must_change_password: true}` when force-change is pending (all privileged REST).
  - `get_authenticated_user` — allows inactive + force-change; **only** for `GET /auth/me` and password change (not for tenant join mutations).
  - `get_current_admin` / `require_role(...)` — built on `get_current_user` (inherit force-change gate).
  - `raise_if_password_change_required(user)` — explicit check when a handler still uses `get_authenticated_user` but must block force-change users.
- Non-Depends auth (WebSocket setup, file download query/Bearer token) **must** use `load_user_from_access_token` (not bare `decode_access_token` + `user_dao.get`). Bare `get` does not join identity, so `must_change_password` would always look false.
- Use async password helpers from async paths; bcrypt work is intentionally pushed to a thread pool.
- Token payloads use `sub` for user id and `role` for role. Force-change is **not** in the JWT; it is re-read from identity on each request.

## Permissions

- `permissions.py` is the shared source of truth for agent visibility, access levels, and relationship access.
- Prefer `list_visible_agents(...)` (DAO). There is no SQLAlchemy visibility query.
- Use `check_agent_access(...)` at HTTP endpoint boundaries.
- Access *decisions* (not agent rows) may be cached in Redis via `access_cache.py`. TTL `AGENT_ACCESS_CACHE_TTL_SECONDS` (0 disables). Policy writes bump `aclver:{agent_id}` **after** the top-level `connection_ctx` commit. `set_cached_level` is skipped if `aclver` changed since compute. Redis errors fail open to DAOs. Do not cache 403/404.
- Agent tool config mutations that change sandbox egress (`allow_network`, proxy fields) are authorized in `app/api/tools.py` (admin roles), not here; keep permission policy for agent visibility separate from sandbox egress policy.
- Use non-HTTP helpers such as `get_agent_access_level_for_user_id(...)` and `user_can_manage_agent_id(...)` in services/background jobs.
- Keep relationship checks in `evaluate_agent_relationship_status(...)` and `evaluate_human_relationship_status(...)`.

## Events

- Use `publish_event(channel, data)` for Redis Pub/Sub events.
- Do not create ad hoc Redis clients for shared app events.
- `close_redis()` is called during app shutdown; event users should not close the process-global client themselves.

## Existing Exceptions

- Bundled skill-creator payloads under `app/services/skill_creator_files/` may still import `loguru`. Do not "fix" those; they run outside the backend process.
