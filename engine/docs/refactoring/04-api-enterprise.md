# 4. `app/api/enterprise.py`

## Snapshot

| Field | Value |
|---|---|
| Rank / composite score | 4 / 15.48 |
| Pure LOC / symbols / max function LOC | 1429 / 62 / 83 |
| Branch nodes | 264 |
| Fan-in / fan-out | 1 / 103 |
| On project god-file list | Route hotspot called out by `app/api/AGENTS.md` |
| Coverage grade | Thin: `tests/test_enterprise_invites.py` covers one slice |
| Analyzed at | `262e123`, 2026-07-05 |

## What This Module Owns Today

This router is an enterprise admin surface rather than one domain:

- Email existence and LLM provider/model management at lines 51-339.
- Enterprise info and approval routes at lines 342-429.
- Audit logs, dashboard stats, tenant quotas, and email templates at lines 430-687.
- Platform/system settings and SSO derived state at lines 688-846.
- Identity-provider and OAuth2 provider management at lines 847-1243.
- Organization departments, members, invites, and WeCom callbacks at lines 1284-1710.

## Why It Hurts Maintainability

The module combines unrelated admin capabilities behind one router: LLM settings, quota policy, system email, SSO platform state, identity providers, org sync, invites, approvals, audit logs, and WeCom callbacks. The common label is "enterprise", but implementation changes require reading many unrelated flows and trust models.

## Coupling Map

- Inbound: mounted from `app/main.py`.
- Outbound: imports 103 targets across auth, tenant settings, LLM model config, system email, identity providers, org sync, audit, invites, and connector callbacks.
- Hidden coupling: role checks differ by route, platform settings can regenerate SSO domains, and connector verification callbacks share the same module as authenticated admin APIs.

## Split Seams

| Current seam | Destination |
|---|---|
| Lines 77-339: LLM provider/model settings | `app/api/enterprise_llm.py` plus `app/services/enterprise_llm.py` |
| Lines 342-429: enterprise info/approvals | `app/api/enterprise_info.py` |
| Lines 430-687: audit/stats/quotas/email templates | `app/api/enterprise_admin.py` and `app/services/tenant_quota_service.py` |
| Lines 688-846: platform settings and SSO derived state | `app/services/sso_settings_service.py` |
| Lines 847-1243: identity providers/OAuth2 | `app/api/enterprise_identity.py` |
| Lines 1284-1710: org departments/members/invites/WeCom callbacks | `app/api/enterprise_org.py` and `app/services/org_sync/` |

## Target Architecture

Either split the route module into focused route modules mounted under the same `/enterprise` prefix, or keep `enterprise.py` as an aggregator and move behavior into focused services. The existing `org_sync/` package is the model for provider-specific organization behavior; identity-provider behavior should stay separate from org sync.

## Migration Order

1. Extract schemas and helper functions that have no side effects.
2. Split identity-provider and org-sync routes first because they already have adjacent registries/packages.
3. Extract LLM model settings into an enterprise LLM service.
4. Extract platform SSO settings after pinning domain-regeneration behavior.
5. Leave audit/stats/quotas as smaller route groups once the provider-heavy paths move.

## Pre-Refactor Characterization Tests

- Given a non-platform admin, when listing LLM models, then only tenant-visible models are returned.
- Given public base URL changes, when settings update, then SSO domain regeneration follows current precedence.
- Given identity provider creation/update, when a tenant admin performs it, then tenant scoping and secret redaction are preserved.
- Given enterprise invites, when invite users runs, then `tests/test_enterprise_invites.py` still passes unchanged.

## Risks

- Auth rules vary between platform admin, org admin, and tenant users; splitting routes can accidentally broaden access.
- Connector callback routes have a different public trust model than admin endpoints.
- Provider secrets and encrypted config must not leak through response schemas.

## Out Of Scope

- Changing endpoint paths or admin roles.
- Merging auth-provider and org-sync registries.
- Redesigning tenant settings storage.

## Acceptance Criteria For The Refactor

- No single enterprise route file exceeds 300 pure LOC.
- Public callback routes are separated or explicitly documented apart from authenticated admin routes.
- Identity-provider and org-sync logic remain separate.
- Enterprise invite tests pass, and new tests cover LLM scoping plus SSO domain regeneration.

## Reproduction

Metric row: `score=15.48`, `pure_loc=1429`, `symbols=62`, `branch_nodes=264`, `max_function_loc=83`, `fan_in=1`, `fan_out=103`.

```bash
awk '!/^[[:space:]]*$/ && !/^[[:space:]]*#/' app/api/enterprise.py | wc -l
```
