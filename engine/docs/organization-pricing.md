# Organization pricing and admission

Research date: November 5, 2026. Currency: USD. Billing basis: monthly per organization. These are proposed MaraClaw commercial terms for approval, not a claim that billing exists. The per-account agent cap and the per-agent daily message cap below are enforced by the engine for organizations provisioned through an approved admission. On 2026-11-13 the organization fee and the additional-account price were halved and rounded down to whole USD (Pro $99 to $49, Max $299 to $149, Ultra $799 to $399; additional accounts $12 to $6, $10 to $5, $8 to $4) because organizations pay for their own LLM inference (see below).

| Tier | Organization / month | Included human accounts | Each additional account / month | Maximum human accounts | Agents per account | Messages per agent / day |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Pro | $49 | 5 | $6 | 25 | 2 | 100 |
| Max | $149 | 15 | $5 | 75 | 5 | 200 |
| Ultra | $399 | 40 | $4 | 200 | 10 | 500 |

One account means one human, including the organization administrator. A digital employee is a separate OpenClaw agent, and the tier sets how many ordinary agents each account may own, organization administrators included. The monthly quote is the organization fee plus the number of accounts beyond the included allocation multiplied by the tier's additional-account price. For example, 7 Pro accounts cost $49 + (2 x $6) = $61/month; 20 Max accounts cost $149 + (5 x $5) = $174/month; 50 Ultra accounts cost $399 + (10 x $4) = $439/month. Each additional account brings its own per-account agent allowance. Requests beyond the account or per-account agent limit need a different plan or a reviewed agreement.

The additional-account price and the maximum human accounts are the shared numeric contract enums `OrganizationAdditionalAccountMonthlyUsd` (Pro 6, Max 5, Ultra 4) and `OrganizationMaxAccounts` (Pro 25, Max 75, Ultra 200) in `app/records/organization_plan.py`; the catalog references the members and plan snapshots must hold exactly these values. This defines the quoted terms only: the engine does not enforce the account maximum or bill additional accounts.

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

- agent creation (`agent_provisioning`; the onboarding assistant is created by web-e through `POST /api/agents/`), against the explicit destination organization, so a platform administrator creating into a tiered organization is capped there only;
- ownership transfer and creator handover (`agent_ownership_transfer`), where the whole incoming batch must fit and refusal is `409 target_quota_exceeded`;
- paid company transfer disposition, where the full incoming ordinary batch is checked against the destination before any revocation or migration write and refusal is `409 transfer_destination_quota_exceeded`. Free transfers seal agents in the source and are never capped; untiered destinations keep their earlier behavior.

Agents already over the cap are never touched, further ownership is refused, and an incoming batch that adds no counted agents (only expired ones) is still accepted. Seeders and bootstrap agents are unchanged. The cap follows the tier: `OrganizationAgentLimit` (`app/records/organization_plan.py`; Pro 2, Max 5, Ultra 10, exported to the frontends as a numeric contract enum) is the only place the numbers live, and enforcement uses the catalog value for the linked tier. Stored plan snapshots must have the current shape; any other shape is refused when read, because a fresh deployment never stores one. `GET /api/organization-plans` lists the same shape. `AdminUserSummary.effective_max_agents` reports the limit that applies to a user (null for an uncapped legacy administrator); `quota_max_agents` is the stored legacy value and is not applied in a tier-governed organization.

## Per-agent daily messages

Each plan carries `daily_messages_per_agent`, the human messages one agent accepts per local day. `OrganizationDailyAgentMessageLimit` (`app/records/organization_plan.py`; Pro 100, Max 200, Ultra 500, exported to the frontends as a numeric contract enum) is the only place the numbers live; the catalog references the members and enforcement reads `plan_for(tier).daily_messages_per_agent`. Like the rest of the plan, a stored snapshot without the field, or with a value outside the enum, is refused when read; there is no default or compatibility parser. The cap counts accepted messages, not tokens or model cost: organizations pay for their own LLM inference through their own model providers, which is why the platform fee was halved. In a tier-governed organization the per-user `quota_message_limit` is not applied (`charge_conversation_quota` returns without charging, as the stored `quota_max_agents` is not applied for agent counts), so this per-agent cap is the only message limit there; organizations without a tier keep the per-user limit. The agent `max_llm_calls_per_day` stays a separate limit.

- **Scope.** Only agents of a tier-governed organization (`tier_for_tenant(agent.tenant_id)` is not `None`, read fresh inside the charging transaction) and never `is_system` agents. An organization without a tier has no cap and no behavior change.
- **System organizations.** Bootstrap makes them tier-governed: MaraClaw is Ultra and KABERs is Pro (`SYSTEM_ORG_TIERS` in `app/services/system_org_seeder.py`). At every start, on fresh and existing databases, `ensure_system_orgs` inserts the missing approved admission row (`<slug>@system.invalid`), refreshes an outdated tier or plan snapshot to the current catalog, writes nothing when the row is current, and aborts startup when two approved rows link to one of them.
- **What is counted.** One unit per human message accepted for the agent, whatever the sender's role (administrators included), charged in the accepting transaction right after the sender's `charge_conversation_quota`, which charges nothing in a tier-governed organization (`app/services/agent_message_quota.py`): web chat (`chat_submissions.accept_submission`), IM channel events (`channels/submissions.accept_channel_event`) and Personal WhatsApp originals (`channels/personal_burst_admission`). Replays charge nothing. Onboarding greetings, schedules, triggers, heartbeats and agent-to-agent turns are not charged.
- **Refusal.** `QuotaExceeded` with `quota_type="agent_daily_messages"`, for example "This agent has reached its daily limit of 100 messages. It resets at midnight (Asia/Ho_Chi_Minh)." Web chat answers `turn_rejected` with reason `quota_exceeded`, and the whole turn rolls back. A Personal WhatsApp original becomes the existing `quota_rejected` outcome. Other channel intake treats it exactly like the per-user quota: the WhatsApp Cloud webhook skips the event and no other provider handler has a quota-specific branch.
- **Day boundary.** The local calendar date in the timezone (`users.timezone`) of the organization's genesis org admin (`role = 'org_admin'` and `is_genesis`); unset, empty or invalid means UTC. Usage is stored with the date it counts, so the first message of a new local date starts from zero; there is no reset job. Changing the admin's timezone moves the boundary immediately.
- **Storage.** `agent_daily_message_usage(agent_id PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE, usage_date DATE, messages_used INTEGER CHECK (messages_used >= 0), updated_at)`, one row per agent. It is created by bootstrap with `CREATE TABLE IF NOT EXISTS`, so a rerun adds it to an existing database with no `ALTER` and no upgrade patch; deleting an agent or its company removes the row. The charge is a single `INSERT ... ON CONFLICT (agent_id) DO UPDATE ... WHERE` statement (`agent_daily_message_usage_dao.charge`): concurrent turns serialize on the row and re-check the cap against the committed count, and no returned row means refused.
- **Admin.** `GET /api/users/{user_id}` reports `daily_messages` (`used`, `limit`, `timezone`, `resets_at`) per agent, and `POST /api/users/{user_id}/agents/{agent_id}/daily-messages/reset` zeroes today's count and writes an `agent_daily_messages_reset` entry to the admin audit trail (`admin-apis.md`).

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
- Storage limits leave the prices and the per-account agent cap as they are.

Like the rest of the plan, stored snapshots must carry both fields; a snapshot without them, or with a value outside the enum, is refused when read, and there is no default or compatibility parser.

Each plan also carries per-agent guest limits and a workspace disk allowance, owned by `OrganizationGuestMemoryBytes`, `OrganizationGuestMillicpus`, `OrganizationGuestCpuShares`, `OrganizationGuestPidsLimit` and `OrganizationWorkspaceDiskBytes` (`app/records/organization_plan.py`) and exported to the frontends as numeric contract enums.

| Tier | Guest memory (= swap) | vCPU | CPU shares | Processes | Workspace disk |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pro | 2 GiB | 1.5 | 1024 | 512 | 2.5 GiB |
| Max | 3 GiB | 2.0 | 1536 | 768 | 5 GiB |
| Ultra | 4 GiB | 3.0 | 2048 | 1024 | 8 GiB |

Unlike the other plan fields, a stored snapshot without these five fields is filled from the current catalog for its tier when read (`OrganizationAdmissionRecord.from_row`), so plans stored before they existed keep loading. The guest limits apply when a guest is created and when a stopped guest resumes; workspace disk is a measured soft quota (host workspace directory plus the guest's writable layer) that refuses new human work while exceeded. It is separate from `storage_limit_bytes`, which counts managed S3 objects only. Enforcement details, system-agent and untiered fallbacks and the resource-pressure records used for upgrade suggestions: [guest-containers.md](guest-containers.md).

The repository initializes database objects from `engine/scripts/schema_baseline.sql`. An existing `organization_admission_requests` table created before named provisioning is upgraded in place on the next bootstrap (rows kept, organization name empty, never provisioned retroactively); no other existing-database migration is provided.
