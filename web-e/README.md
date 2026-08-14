# web-e — end-user product UI

Register, sign in, and join an organization. Chat / agent workspace is not in this package yet.

```bash
cd web-e && npm install && npm run dev
```

Dev server is http://localhost:5175 and proxies `/api` to the engine (`VITE_DEV_API_PROXY`, default `http://127.0.0.1:8000`).

- `/register` — `POST /api/auth/register/init`
- `/join` — confirm a claimed email domain or continue in OpenClaw
- `/login` — resumes pending org confirm when `needs_org_confirm` is set
