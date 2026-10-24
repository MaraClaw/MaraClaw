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
