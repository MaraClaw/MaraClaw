# app/services/org_sync

Provider-specific organization-structure sync adapters live here. This package is related to identity providers, but it is not the auth-provider login registry.

## Where To Look

| Task | Location | Notes |
|---|---|---|
| Sync orchestration | `base.py` | `BaseOrgSyncAdapter.sync_org_structure()` fetches departments/users, records partial failures, and reconciles only after a clean sync |
| Provider factory | `factory.py` | `SYNC_ADAPTER_CLASSES` supports `feishu`, `dingtalk`, `wecom`, `google_workspace` |
| Department reconciliation | `departments.py`, `paths.py` | Provider-scoped department upserts, virtual root `external_id == "0"`, path rebuilds, recursive member counts |
| Member reconciliation | `members.py` | Upserts org members, links existing platform users by email/phone, and syncs contact fields |
| Provider adapters | `feishu.py`, `dingtalk.py`, `wecom.py`, `google_workspace.py` | API token fetching and provider payload mapping |
| Public service entry | `app/services/org_sync_service.py` | Loads `IdentityProviderRecord`, requires tenant binding. No outer commit wrapper — each mixin `connection_ctx` commits unless the caller already opened one. |

## Conventions

- New providers must subclass `BaseOrgSyncAdapter`, implement `api_base_url`, `get_access_token()`, `fetch_departments()`, and `fetch_users()`, then register in `factory.py`.
- Provider config comes from `IdentityProvider.config`; tenant scoping comes from the provider row or explicit factory argument.
- `sync_org_structure()` fetches provider HTTP **outside** a DB transaction, then upserts departments/members on a shared `connection_ctx` per batch. Nested `connection_ctx()` **joins** (no savepoints). Preserve partial-failure collection and skip stale-record reconciliation after any partial failure.
- Feishu and DingTalk users require `unionid`; `unionid` must not equal `external_id`.
- Existing member lookup prefers provider-safe stable identifiers; be careful changing fallback order between `unionid`, `external_id`, and open-id fields.
- Department paths are derived from the internal parent tree; virtual provider root `external_id` `"0"` maps to an empty path.
- Stale departments and members are marked deleted only after a non-partial sync.
- Member sync does not create platform users; it links existing `UserRecord` rows by email/phone and may sync email/mobile on the linked identity. `user_created` stays false.

## Avoid

- Do not confuse `provider_type` auth-provider support with org-sync provider support; the registries intentionally differ.
- Do not reconcile stale records after a sync that had fetch/upsert errors.
- Do not add provider-specific identity shortcuts without checking connector semantics in `app/services/AGENTS.md` and API callback trust rules.
- Do not create child docs under `providers/`; that directory is not an implemented provider package today.
