# app/services/linkup

Engine-side Linkup key ring and HTTP proxy. Official skill packages are bundled
in the OpenClaw guest image; this package does not add function-calling tools.

- `keys.py` - add/remove/list, cursor, quota cooldown
- `client.py` - forward to api.linkup.so and rotate on quota; threads `agent_id` into analytics
- `jobs.py` - bind async research/extract ids to one key
- `tokens.py` - guest proxy token (not a real Linkup secret)
- `analytics.py` - billed POST → `web_search_events` (normalized + HMAC, no raw). Must not fail the guest.
- `export.py` - worker drain to `ANALYTICS_S3_PREFIX` (never workspace `agents/`)

## Proxy behavior

- Only `search`, `fetch`, `research` and `extract` paths are forwarded; the proxy always replaces `Authorization` with the selected key.
- A successful `POST research|extract` binds the returned upstream job id to the key that created it (`bind_job`).
- `GET research|extract/{id}` uses only that bound key, with no rotation, env fallback or retry. An unknown job or a removed key fails `410` (`LinkupJobKeyRemovedError`). These polls are not written to analytics.
- Other calls start at the ring's current key. Quota (402, or 429 whose body carries a quota marker) marks the key exhausted, advances the cursor and tries the next key; each key is tried once per call. A transient failure (5xx or 408) retries the same key once and does not burn the next key; the retry allowance resets after a quota rotation.
- With no stored key the call uses env `LINKUP_API_KEY` (seeded into the ring when possible); with neither it fails `503`. A transport failure is `502`.
