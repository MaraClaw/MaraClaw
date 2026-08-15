# app/services/linkup

Engine-side Linkup key ring and HTTP proxy. Official skill packages stay in
`linkup_skill_files/`; this package does not add function-calling tools.

- `keys.py` - add/remove/list, cursor, quota cooldown
- `client.py` - forward to api.linkup.so and rotate on quota; threads `agent_id` into analytics
- `jobs.py` - bind async research/extract ids to one key
- `tokens.py` - guest proxy token (not a real Linkup secret)
- `analytics.py` - billed POST → `web_search_events` (normalized + HMAC, no raw). Must not fail the guest.
- `export.py` - worker drain to `ANALYTICS_S3_PREFIX` (never workspace `agents/`)
