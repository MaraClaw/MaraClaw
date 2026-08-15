# app/services/linkup

Engine-side Linkup key ring and HTTP proxy. Official skill packages stay in
`linkup_skill_files/`; this package does not add function-calling tools.

- `keys.py` - add/remove/list, cursor, quota cooldown
- `client.py` - forward to api.linkup.so and rotate on quota
- `jobs.py` - bind async research/extract ids to one key
- `tokens.py` - guest proxy token (not a real Linkup secret)
