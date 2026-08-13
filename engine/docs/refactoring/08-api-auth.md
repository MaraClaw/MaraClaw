# 8. `app/api/auth.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 8 / 10.54 |
| Pure LOC / symbols / max function LOC | 902 / 26 / 138 |
| Branch nodes | 170 |
| Fan-in / fan-out | 4 / 79 |
| On project god-file list | Route hotspot called out by `app/api/AGENTS.md` |
| Coverage grade | Good for core auth: `tests/test_auth.py`, `tests/test_auth_provider.py`, `tests/test_sso_toggle.py`, plus related GOGCLI/org-sync tests |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

The module owns username/password registration, login, email verification, password reset, tenant switching, OAuth/SSO authorization, OAuth callbacks, identity binding, and account updates:

- Registration config and duplicate checks at lines 48-116.
- `register_init()` at lines 140-254 and normal registration helper at lines 305-413.
- `login()` at lines 424-543.
- Password reset and profile routes at lines 584-835.
- SSO/OAuth routes and Redis pending-token helpers around lines 838-1120.
- Email verification endpoints at lines 1141-1235.

## Why It Hurts Maintainability

Authentication has several trust boundaries in one route file: local credential registration, email verification, password reset, tenant selection, cross-domain tenant switching, provider OAuth, and identity binding. Some logic already belongs in services (`registration_service`, `password_reset_service`, `auth_registry`), but the route still performs multi-step state transitions and Redis pending-token orchestration.

## Coupling Map

- Inbound: mounted from `app/main.py`; referenced by OKR, plaza, and triggers for auth dependencies.
- Outbound: imports 79 targets across security, DAOs, tenants, email verification, password reset, registration, auth providers, Redis, and schemas.
- Hidden coupling: OAuth two-step pending tokens in Redis, first-user/default-tenant behavior, email verification fallback when SMTP is absent, and cross-domain tenant switching redirects.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 140-413: registration flow | `app/services/registration_service.py` expansion or `app/services/auth_registration.py` |
| Lines 424-543: login flow | `app/services/auth_login.py` |
| Lines 584-835: password/profile/tenant switching | `app/services/password_reset_service.py` and `app/services/tenant_session_service.py` |
| Lines 838-1120: OAuth authorize/callback/bind | `app/services/oauth_flow.py` |
| Lines 1141-1235: email verification | `app/services/email_verification_service.py` |

## Target Architecture

Keep route handlers as boundary parsers and response mappers. Reuse `auth_registry.py`, `auth_provider.py`, `registration_service.py`, `password_reset_service.py`, and `email_verification_service.py` instead of adding new auth logic to the route module.

## Migration Order

1. Extract OAuth pending-token cache helpers into `oauth_flow.py` with tests.
2. Move OAuth authorize/callback/bind orchestration behind service methods while preserving response unions.
3. Move tenant switching/session behavior into a tenant session service.
4. Collapse registration routes onto `registration_service` after pinning first-user/default-tenant behavior.
5. Leave route models and response mapping until the end.

## Pre-Refactor Characterization Tests

- Given first user registration with no tenant, when registration completes, then default tenant and verification behavior match current tests.
- Given SMTP is missing, when login sees an unverified account, then auto-verification/403 behavior remains unchanged.
- Given OAuth returns multiple tenant memberships, when callback runs, then pending token caching and tenant choices match current response shape.
- Given tenant switch request, when cross-domain switching is needed, then token/redirect behavior is unchanged.

## Risks

- Response models include unions such as token response vs multi-tenant response; route behavior must remain exact.
- OAuth pending-token cache must stay single-use and short-lived.
- Auth routes are security-sensitive; do not hand-roll role checks or expose secrets.

## Out Of Scope

- Changing auth provider contracts.
- Redesigning tenant membership or SSO domain behavior.
- Changing JWT claims or token expiration.

## Acceptance Criteria For The Refactor

- `app/api/auth.py` stays below 300 pure LOC.
- OAuth, registration, login, password reset, and tenant switching have service-level characterization tests.
- Existing auth/provider/SSO tests pass unchanged.
- No provider secrets, encrypted config, cookies, or hashes appear in responses.

## Reproduction

Metric row: `score=10.54`, `pure_loc=902`, `symbols=26`, `branch_nodes=170`, `max_function_loc=138`, `fan_in=4`, `fan_out=79`.

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/api/auth.py | wc -l
```
