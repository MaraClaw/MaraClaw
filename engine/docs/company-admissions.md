# Applicant admission history

## Summary-page API

`GET /api/company-admissions/mine/page` requires the ordinary bearer-token
`get_current_user` authentication dependency: active membership and identity,
current token generation, and no required password change. A fresh principal
check also runs before cursor decoding. A tenant is not required; ownership is the authenticated
membership's `user.id`, not its identity or current tenant.

| Query | Contract |
|---|---|
| `status` | Optional: `pending`, `approved`, `rejected`, or `withdrawn`. Omit for all statuses. `all`, `null`, empty, and unknown strings are invalid. |
| `limit` | Integer, 1–100 inclusive; default 50. May change between pages. |
| `cursor` | Optional opaque string, at most 2048 characters. Omit on the first page; pass the returned `next_cursor` unchanged for older requests. |

The response is always an object with both keys, including on an empty page:

```json
{
  "items": [
    {
      "id": "00000000-0000-0000-0000-000000000001",
      "status": "rejected",
      "source_tenant_id": null,
      "target_tenant": {
        "id": "00000000-0000-0000-0000-000000000002",
        "name": "Example company"
      },
      "method": "verified_domain",
      "created_at": "2026-10-21T12:00:00.123456Z",
      "decided_at": "2026-10-22T12:00:00.123456Z",
      "decision_reason": null
    }
  ],
  "next_cursor": null
}
```

`items` contains `RequestSummary` objects, not reviewer details. The eight shown
fields are required; `source_tenant_id`, `decided_at`, and `decision_reason` are
nullable. IDs are UUID strings. `method` is `invitation`, `verified_domain`, or
`sso`. Timestamps are ISO 8601 values with timezone; preserve microseconds when
transporting cursor values. `target_tenant` has required UUID `id` and string
`name`. Applicant identity/email, source-tenant details, and reviewer IDs are not
included in a summary.

Requests are ordered by `created_at DESC, id DESC`. The DAO fetches `limit + 1`
rows and returns an exclusive `(created_at, id)` seek position only when another
row exists. Consequently, an exactly full final page has `next_cursor: null`,
and an empty result is `{"items": [], "next_cursor": null}`. No total count is
returned.

## Cursor and errors

The separate `ApplicantCursor` validates required version `1`, kind `applicant`,
owner `user_id`, canonical `status` (JSON null for all), timezone-aware
`created_at`, and UUID `id`. Unknown fields and coerced types are rejected. It
is not a reviewer cursor. The base64url position is unsigned and is not an
authorization credential: every query still enforces the authenticated user ID.
Clients must treat it as opaque, rather than reconstructing it from displayed
timestamps.

- Malformed base64/UTF-8/JSON, invalid fields, wrong version/kind, foreign owner,
  or a filter mismatch: **400**, exactly `{"detail": "Invalid cursor"}`.
- Invalid status/limit or a cursor longer than 2048 characters: **422** using
  the ordinary FastAPI validation-error response.
- Missing, invalid, revoked, or inactive sessions: the ordinary **401** auth
  response. Required password change: the ordinary **403** response with
  `detail.must_change_password: true`.

Reset to a cursorless root when owner or status changes. Omit `status` again
for every continuation of an all-status page. Root refreshes read current
state. On a later-page cursor error, the UI can offer a return to the first
page. Traversal is not a snapshot: decisions or withdrawals may change filtered
results between requests, and newer rows above the boundary appear on refresh.
Reading history does not change membership, session, invitations, or decisions.

## Existing contracts

The offset-based `GET /mine` still returns a summary array. `GET /review`,
`GET /review/page`, request-detail access, decisions, and idempotent withdrawal
retain their existing contracts. Reviewer pages still use tenant/genesis scope
and detail objects; applicant pages do not grant reviewer privileges.

Regression coverage lives beside the existing review and withdrawal tests:
`test_company_admission_applicant_cursor.py`,
`test_company_admission_applicant_page.py`, and
`test_company_admission_applicant_postgres.py`. The latter two exercise the real
ASGI/auth/DAO stack against unique PostgreSQL schemas when
`BOOTSTRAP_TEST_DATABASE_URL` is set; they skip otherwise.

## Default-organization transfer contract

Only an active default-end-user-organization member may move to a different
company. Company-to-company moves are forbidden, including through invitation,
domain-join and SSO entrypoints. Same-company login/join behavior is unchanged;
tenantless and newly registered applicants without agents need no paid request.

`GET /api/company-admissions/transfer-quote` uses ordinary authenticated member
access, **not** admin access. It returns `fee_amount_minor` (integer minor units),
`currency` (three-letter currency), `instructions`, `source_eligible`,
`has_owned_agents`, `explicit_choice_required`, and
`free_requires_destructive_consent: true`. A quote is informational; the server
snapshots the current policy when the transfer is submitted. It never accepts
a client-supplied price or instructions.

`POST /api/tenants/transfer` preserves `password` confirmation and existing
`invitation_code` / `tenant_id` destination verification. Additional fields:

| Field | Contract |
|---|---|
| `transfer_choice` | `paid` or `free`. Required for a default-org applicant who owns agents. Omission is permitted only for applicants without agents. |
| `free_destructive_consent` | Boolean, default false. Must be true with explicit `free`; persisted before admission creation. |

An explicit paid submission returns **202**
`{"status":"payment_review","transfer_request":TransferSummary}`.
**No admission exists yet**, no destination approval is requested, and no assets
are moved or sealed. An explicit free submission returns the existing **202**
`{"status":"pending","request":RequestSummary}`. An implicit agent-free
submission also returns that pending-admission shape, without inventing a paid
transfer. Free consent is not executed until the destination's final approval.

`TransferSummary` contains required UUID `id`, `applicant_user_id`,
`source_tenant_id`, `target_tenant_id`; `choice` (`paid` / `free`); `status`
(`payment_review`, `admission_created`, `rejected`, `withdrawn`); integer
`fee_amount_minor`; string `currency` and `instructions`; timezone-aware
`created_at`; nullable timestamps `free_consent_at`, `payment_confirmed_at`,
`reviewed_at`; nullable UUIDs `reviewed_by_user_id`, `admission_request_id`; and
nullable string `review_reason`. Policy, destination, source, applicant, choice
and free consent are immutable snapshots. Once an admission exists its final
status is read from the ordinary admission APIs, not from payment status.

| Route | Response / authorization |
|---|---|
| `GET /api/company-admissions/transfers/mine?limit=50` | `TransferSummary[]`, applicant-owned; limit 1–100. |
| `GET /api/company-admissions/transfers/review?limit=50` | `TransferSummary[]`; payment reviewers only; limit 1–100. |
| `GET /api/company-admissions/transfers/{id}` | `TransferSummary`; owner or payment reviewer. |
| `POST /api/company-admissions/transfers/{id}/decision` | `{decision:"approved"\|"rejected",reason?:string\|null}` (max 2000 characters); returns `TransferSummary`. |
| `POST /api/company-admissions/transfers/{id}/withdraw` | Owner only; returns `TransferSummary`. Once an admission exists, withdraw that admission instead. |

Payment reviewers are active platform admins or the active **default org's
genesis org admin**. Additional org admins and destination genesis admins do not
gain payment-review privileges. Destination admission decisions retain their
existing, separate target-company genesis-admin authorization.

Payment approval atomically creates a pending admission, associates its ID,
and writes both the target review notification and applicant creation notice.
The applicant email is scheduled only after commit and says admission was
created and awaits target approval. Rollback sends no email; repeat approval
returns the same admission ID without duplicate admissions or notifications.
Payment rejection/withdrawal and admission rejection/withdrawal do not destroy
or migrate assets.

Admission summaries/details add nullable `transfer`:
`{transfer_request_id,choice,fee_amount_minor,currency,free_consent_at}`.
`transfer:null` identifies legacy/agent-free admission without explicit choice.
Clients must handle both forms. Creating agents while such an admission is
pending requires withdrawing and resubmitting with an explicit choice before
final approval can succeed.

Structured workflow errors use `detail.code`: **409**
`transfer_choice_required`, **409** `transfer_already_pending` (includes
`transfer_request_id`), and **422** `free_destructive_consent_required`.
The ordinary destination, authentication, password and decision-conflict errors
remain in force. A non-default source is **409**.

## Final approval and retained data

Only the target company's genesis administrator makes the final admission
decision. The server rechecks source eligibility, submitted disposition and
current agent ownership under tenant/user/agent locks. An implicit agent-free
request cannot approve if the applicant has since created agents. Workspace
flocks fence setup and OpenClaw cutover through the owning transaction's commit
or rollback; an active OpenClaw update prevents final disposition.

Paid approval preserves agent IDs, user-owned integration configurations and
content. It moves operational agent/public-page tenant attribution, removes
foreign-tenant model and source-only tool bindings and old organization grants,
and stops runtime/automation. Historical chats, tasks, audit/activity, usage and
Plaza attribution are not rewritten. Runtime configuration is regenerated using
only destination models; source pool credentials are scrubbed before member
access resumes.

Free approval does **not** call generic agent deletion. It leaves source-tenant
agent rows as private, expired, stopped FK anchors with no engine key, model
bindings, heartbeat or autonomy access. Their creator becomes a deterministic,
inactive, identityless, tenantless retention holder. The restrictive
`company_transfer_retained_agents` ledger records original applicant, source,
request and holder, and prevents accidental generic agent deletion. Integrations,
permissions, relationships in both directions, schedules, triggers, public
pages and credentials are removed. Task reminders and agent-derived user
notification access are neutralized. Chats, tasks, audit/activity, accounting,
Plaza, workspace revisions, files and memory remain retained with source
attribution; former-user IDs are not bulk-reassigned in historical rows.

`company_transfer_runtime_cleanup` durably records pending container and
credential-artifact teardown. After commit, cleanup uses exact owned-container
removal and per-agent workspace/update exclusion, never whole-workspace removal
or archive moves. Failures remain pending for the existing stopped-container
worker's reconcile retry. Until cleanup completes, agent access and guest writes
return **423** `{"detail":{"code":"transfer_cleanup_pending","retryable":true}}`.
Final approval and its data changes remain committed if this asynchronous
cleanup needs retry. Creator-keyed inbox, approval and quota queries are scoped
to current membership tenant; sealed notification references are excluded even
if an already-running producer writes a late notice.

The three transfer tables are additive greenfield baseline DDL only. No existing
installation upgrade patch is supplied. Opt-in PostgreSQL tests require an
explicitly isolated `BOOTSTRAP_TEST_DATABASE_URL`; ordinary local QA uses fakes
and does not connect to a database, Docker daemon or real email service.
