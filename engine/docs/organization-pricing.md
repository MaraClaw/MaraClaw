# Organization pricing and admission

Research date: November 5, 2026. Currency: USD. Billing basis: monthly per organization. These are proposed MaraClaw commercial terms for approval, not a claim that billing exists. The per-account agent cap below is enforced by the engine for organizations provisioned through an approved admission.

| Tier | Organization / month | Included human accounts | Each additional account / month | Maximum human accounts | Agents per account |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pro | $99 | 5 | $12 | 25 | 2 |
| Max | $299 | 15 | $10 | 75 | 5 |
| Ultra | $799 | 40 | $8 | 200 | 10 |

One account means one human, including the organization administrator. A digital employee is a separate OpenClaw agent, and the tier sets how many ordinary agents each account may own, organization administrators included. The monthly quote is the organization fee plus the number of accounts beyond the included allocation multiplied by the tier's additional-account price. For example, 7 Pro accounts cost $99 + (2 x $12) = $123/month; 20 Max accounts cost $349/month; 50 Ultra accounts cost $879/month. Each additional account brings its own per-account agent allowance. Requests beyond the account or per-account agent limit need a different plan or a reviewed agreement.

The additional-account price and the maximum human accounts are the shared numeric contract enums `OrganizationAdditionalAccountMonthlyUsd` (Pro 12, Max 10, Ultra 8) and `OrganizationMaxAccounts` (Pro 25, Max 75, Ultra 200) in `app/records/organization_plan.py`; the catalog references the members and plan snapshots must hold exactly these values. This defines the quoted terms only: the engine does not enforce the account maximum or bill additional accounts.

The customer configures organization model providers and pays their inference charges separately. Third-party paid tools and services are also separate. The platform price does not include model tokens, promise unlimited work, or promise dedicated hardware, uptime or support service levels.

## Benchmarks and rationale

Official pricing pages accessed on the research date:

| Product | Observed billing model | Implication for MaraClaw |
| --- | --- | --- |
| [Lindy](https://www.lindy.ai/pricing) | Starts at $29.99 per active user/month with 3,000 credits per seat pooled across the workspace. Credit top-ups cost $10 per 1,000. | Seats make collaboration easy to quote, but bundled usage is a different offer from MaraClaw's customer-paid inference. |
| [Dust](https://dust.tt/home/pricing) | Business Pro seats are EUR 30/month, or EUR 24/month billed yearly, with 8,000 monthly credits. Max seats are EUR 150/month, or EUR 120/month billed yearly, with 40,000 credits. | Explicit monthly versus annual terms prevent misleading comparisons. Higher usage costs more; no unlimited-compute claim is needed. |
| [n8n](https://n8n.io/pricing) | Hosted Pro is EUR 50/month billed annually, with 10,000 monthly workflow executions and unlimited users. | An organization capacity fee is a valid alternative to pure per-seat pricing; collaborators and execution capacity need not share one meter. |

These products have different usage units, included inference, hosting models and currencies. No currency conversion or claim of equivalent capability is made. Competitor prices are observations; all MaraClaw prices and allocations above are product recommendations.

An organization base fee pays for platform operation and bounded hosted agent capacity. Lower additional-account prices encourage teams to collaborate without pretending that a human account incurs the same cost as an isolated guest. The larger tiers bundle more human accounts and a higher per-account agent cap rather than offering unmeasured compute discounts.

Margins are unknown without measured guest CPU, memory, storage, active time, network and support costs. Before activating paid organizations, measure those costs and agree workload/resource boundaries. Model-provider charges remain separate in every tier. No annual discount, automatic overage charge, payment collection or additional-seat purchase flow is defined by this change.

## Admission contract

The public landing page collects the organization name, a contact email and a desired tier. Submission creates a pending organization request for the active genesis platform administrator. It does not verify the email, claim a domain, create a tenant or administrator, accept payment, or activate a subscription. A duplicate pending request receives the same public acknowledgement and retains its original name, email and tier.

Rejection records the administrator's decision and optional note; nothing else happens. Approval means commercial review has passed and provisions the organization: one transaction creates the tenant named after the request and its genesis organization administrator for the request email (email-verified, claiming the email's domain as the default domain exactly as company creation does, with a generated one-time password that must be changed at first sign-in). An email that already has an account, a domain another company already claimed, or a legacy request without an organization name cannot be approved (409, nothing created, request stays pending); an existing account is never reset or taken over. The initial credentials reach the contact only by email, sent after commit over the platform SMTP, and are retried durably; the administrator console shows pending, sent or failed delivery (SMTP acceptance is not an inbox guarantee) and can re-queue a failed delivery without approving again or changing the password. If the administrator changes the password first, the original credentials are not sent. The stored quote is a snapshot so later catalog changes do not silently change submitted terms.

The account allocations and prices are proposed commercial terms; this request flow adds no seat enforcement, billing or payment collection. Existing member invitations and company membership admissions retain their own lifecycle.

## Per-account agent cap

An organization is tier-governed when an approved admission request has `provisioned_tenant_id` equal to it; the request's `tier` is the authority and is read fresh inside the creating or transferring transaction. No new column or migration exists. An organization with no such link keeps the earlier rules: the stored per-user `quota_max_agents`, with platform and organization administrators exempt, counting the creator's non-expired agents in their own organization (system agents included). Two approved requests linked to one organization are a configuration error, and the operation fails instead of falling back.

In a tier-governed organization the cap is the current catalog value for the tier (Pro 2, Max 5, Ultra 10) for every role, platform and organization administrators included, and it replaces the stored `quota_max_agents` (an inherited 2 never lowers Max or Ultra). The count is the creator's non-expired, non-system agents whose `tenant_id` is that organization, whatever their lifecycle status (stopped, failed setup and pending deletion all count). It is enforced under the shared tenant and user locks by:

- agent creation and onboarding (`agent_provisioning`), against the explicit destination organization, so a platform administrator creating into a tiered organization is capped there only;
- ownership transfer and creator handover (`agent_ownership_transfer`), where the whole incoming batch must fit and refusal is `409 target_quota_exceeded`;
- paid company transfer disposition, where the full incoming ordinary batch is checked against the destination before any revocation or migration write and refusal is `409 transfer_destination_quota_exceeded`. Free transfers seal agents in the source and are never capped; untiered destinations keep their earlier behavior.

Agents already over the cap are never touched, further ownership is refused, and an incoming batch that adds no counted agents (only expired ones) is still accepted. Seeders and bootstrap agents are unchanged. The cap follows the tier: `OrganizationAgentLimit` (`app/records/organization_plan.py`; Pro 2, Max 5, Ultra 10, exported to the frontends as a numeric contract enum) is the only place the numbers live, and enforcement uses the catalog value for the linked tier. Stored plan snapshots must have the current shape; any other shape is refused when read, because a fresh deployment never stores one. `GET /api/organization-plans` lists the same shape. `AdminUserSummary.effective_max_agents` reports the limit that applies to a user (null for an uncapped legacy administrator); `quota_max_agents` is the stored legacy value and is not applied in a tier-governed organization.

## Per-agent managed storage and file size

Each plan also carries `storage_limit_bytes` and `max_file_size_bytes`, whole integer bytes owned by `OrganizationStorageLimitBytes` and `OrganizationFileLimitBytes` (`app/records/organization_plan.py`) and exported to the frontends as numeric contract enums. The catalog (`app/services/organization_plans.py`) references the enum members; enforcement reads `plan_for(tier).storage_limit_bytes` and `.max_file_size_bytes`.

| Tier | Storage per agent | Largest managed file |
| --- | ---: | ---: |
| Pro | 1073741824 (1 GiB) | 26214400 (25 MiB) |
| Max | 5368709120 (5 GiB) | 52428800 (50 MiB) |
| Ultra | 10737418240 (10 GiB) | 104857600 (100 MiB) |

Scope of these numbers:

- `storage_limit_bytes` is per agent: the logical bytes of that agent's current managed S3 objects. Direct guest or local-disk writes and historical S3 object versions are not counted.
- `max_file_size_bytes` is the largest single managed file, not the size of any individual HTTP upload.
- This is not a limit on all workspace storage, and it does not make every API upload accept 100 MiB. Individual upload tools and document-parser paths can enforce stricter limits of their own, and the global operation upload maximum is unchanged.
- Prices and the per-account agent cap are unchanged.

Like the rest of the plan, stored snapshots must carry both fields; a snapshot without them, or with a value outside the enum, is refused when read, and there is no default or compatibility parser.

The repository initializes database objects from `engine/scripts/schema_baseline.sql`. An existing `organization_admission_requests` table created before named provisioning is upgraded in place on the next bootstrap (rows kept, organization name empty, never provisioned retroactively); no other existing-database migration is provided.
