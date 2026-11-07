# app/services/tavily

Engine-side Tavily key ring and HTTP proxy. Guest skills use this proxy; this package does not add function-calling tools.

- `keys.py` - add/remove/list, cursor, quota cooldown
- `client.py` - forward to api.tavily.com and rotate on quota (432/433); threads `agent_id` into analytics
- `http_client.py` - the Tavily-only shared `httpx.AsyncClient`: one per event loop, explicit limits, no redirects, a cookie jar that stores and replays nothing. `shutdown_tavily_client()` closes it from lifespan `stop()`. Never `async with` it, and never reuse `outbound_client()` or `llm/http_pool.py` (that one follows redirects)
- `tokens.py` - guest proxy token (not a real Tavily secret)
- `analytics.py` - billed POST → `web_search_events` (normalized + HMAC, no raw). Must not fail the guest.
- `export.py` - worker drain to `ANALYTICS_S3_PREFIX` (never workspace `agents/`)

## Proxy behavior

- The guest-facing mount stays `/api/tavily/v1/*`; the `/v1` segment is never sent upstream. Only `POST search` and `POST crawl` are forwarded; any other path is `404` and a wrong method is `405`. The proxy always replaces `Authorization` with the selected key. Both operations are synchronous. Crawl uses a singular `url`, optional `instructions`, `extract_depth`, `format`, and `timeout`; its response includes `base_url`, `results`, `response_time`, and `request_id`. The proxy deadline remains authoritative.
- Calls start at the ring's current key. Quota (Tavily 432 key/plan limit, 433 pay-as-you-go limit) marks the key exhausted, advances the cursor and tries the next key; each key is tried once per call. A 429 is a rate limit and 434 is flex-tier capacity: both pass through (status and body; the route does not forward `Retry-After`) without exhausting or rotating. A transient failure (5xx or 408) retries the same key once and does not burn the next key; the retry allowance resets after a quota rotation.
- With no stored key the call uses env `TAVILY_API_KEY` (seeded into the ring when possible); with neither it fails `503`. A transport failure is `502`.
- Every attempt, retry, rotated key and redirect hop goes through the shared client. Each hop's timeout is `min(60s, deadline.remaining())`, set on the request itself, so a wait for a pooled connection spends the call deadline and a pool timeout is the same `502`.
- Upstream `Set-Cookie` is never kept. httpx wraps a `Cookies` subclass in a plain jar, so the guard is a `CookieJar` subclass handed to the client.
- The proxy route resolves the agent's nullable `tenant_id` while checking its token (`tavily_proxy_dao.agent_available` returns `None` when unavailable) and passes it to `proxy_tavily`. Analytics uses it, and falls back to an agent lookup only when it is absent. A tenantless agent is still available.
