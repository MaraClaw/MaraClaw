# types/ - member DTO adapters and admission schemas

Parent: [lib guide](../AGENTS.md), which owns endpoint/client and generated-contract guidance. This directory owns local wire-shape adaptation and runtime schema composition.

## WHERE TO LOOK

| Source | Local schema seam |
|--------|-------------------|
| `auth.ts` | User/token defaults, login/join unions, response guards and password-change flags |
| `company-admissions.ts` | Shared summary/detail shapes, applicant paging and admission filters |
| `company-transfers.ts` | Fee/consent fields, quote/payment-review shapes, transfer paging and input types |

## AUTH ADAPTATION

- `userOutSchema` defaults missing email/tenant to null and email verification to false; it strips unknown fields. `UserOut` deliberately makes selected generated user fields optional.
- `memberTokenSchema` defaults user to null; identity may be absent or null, message is optional, and admission_request may be absent or null. Preserve those distinctions in `TokenResponse`.
- Register adaptation requires the local user schema. Multi-tenant adaptation fixes `requires_tenant_selection` to literal true; login is the union of token and multi-tenant schemas.
- `joinOrgResponseSchema` has approved, pending and immediate-join branches. Approved refines the nested request status to approved; the pending branch reuses the summary schema. Immediate join carries tenant, role and a nullable access_token.
- `isMultiTenantResponse`, `isTokenResponse` and `isNeedsVerificationDetail` are structural branch guards, not full schema parsers. Keep runtime parsing at the consuming client boundary.
- `sessionMustChangePassword` checks the token, user and identity flags; checking only the nested user loses a valid forced-change signal.

## ADMISSION AND TRANSFER SHAPES

- Admission summary/detail share one base object; detail adds applicant, nullable source tenant and nullable deciding-user ID. Keep `.readonly()` on exposed object/array schemas.
- The summary's transfer disposition is both nullable and optional. Older responses may omit it; absence does not mean a paid transfer.
- Admission IDs and tenant summary IDs require nonempty strings; transfer identifiers use UUID validation. Preserve the existing distinction.
- Both page shapes return readonly items and nullable next_cursor; a present cursor is nonempty and at most 2048 characters. A null cursor ends traversal.
- Admission page request status, limit and cursor are optional. Transfer page request limit is required. Both limit schemas accept integers from 1 through 100.
- Fee fields require nonnegative integer minor units and a three-letter uppercase currency. Transfer quote requires `free_requires_destructive_consent: true`.
- `TransferSelection` distinguishes paid from free; free requires `free_destructive_consent: true`. `TransferInput` is the broader request type with password and optional destination/choice fields.
- Payment review has its own `status: 'payment_review'` response with transfer_request. Keep it distinct from the pending/approved admission branches.

## CHANGE BOUNDARIES

- Change the schema and its inferred type together; keep explicitly adapted types consistent with their schema defaults and nullability.
- Reuse the existing summary and transfer-disposition schemas across auth/admission responses instead of copying their fields.
- Preserve schema-specific stripping and readonly behavior; don't make every response strict or nullable as a blanket rule.
- Enum values come from the generated admission method/status and transfer choice/status contracts. Extend the owning contract before teaching a local union a new wire value.
