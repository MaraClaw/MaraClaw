# web-e

End-user product UI. Talks to `engine` with member auth.

## Owns

Register, login, and organization join confirm. Do **not** put admin company controls here (those are `web-a`). Do **not** put marketing here (`web-l`).

## Stack

React 19 + Vite + Tailwind v4. Port **5175**. JWT in `localStorage` (`maraclaw-enduser-token`).

## Org join

Platform admin is a **web-a** account. Do not treat `PLATFORM_ADMIN_EMAIL` as a web-e member login; genesis credentials already in the database win over the env email.

Registration may return `needs_org_confirm` + `suggested_org`. The join screen refreshes via `GET /api/tenants/lookup-by-email` and then calls `POST /api/tenants/join-suggested` or `POST /api/tenants/join-default`. Register accepts an optional invitation code. Signed-in members transfer with `POST /api/tenants/transfer` (password + invite **or** email-domain / OpenClaw destination). Domain join/transfer only applies after the email is verified.
