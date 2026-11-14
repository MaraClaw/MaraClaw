# Organization pricing and admission

Research date: November 5, 2026; re-priced November 14, 2026. Currency: USD. Billing basis: monthly per organization. These are MaraClaw commercial terms. The engine runs one payment workflow, manual bank-transfer orders for extra accounts and agents reviewed by platform admins ([Buying extra accounts and agents](#buying-extra-accounts-and-agents)); it collects nothing else. The per-account agent cap, the company-wide account and agent caps (the tier's included allocation plus approved purchased extras) and the per-agent daily message cap below are enforced by the engine for organizations provisioned through an approved admission; the organization fee, the annual price and the storage and workspace-disk add-ons are quoted terms only.

On 2026-11-14 the terms were re-priced around agents, because guest compute per agent is the main platform cost and the earlier per-account prices ($49 / $149 / $399, additional accounts $6 / $5 / $4, with 2 / 5 / 10 agents per account) lost money once a company created the agents it was allowed. The cost model, the AWS deployment constraints and the margin estimates are in [aws-deployment-pricing.md](aws-deployment-pricing.md).

| Tier | Organization / month | Organization / year | Included agents | Each additional agent / month | Included human accounts | Each additional account / month | Agents per account | Messages per agent / day |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Pro | $79 | $790 | 2 | $35 | 5 | $5 | 2 | 100 |
| Max | $249 | $2,490 | 5 | $49 | 15 | $4 | 3 | 200 |
| Ultra | $699 | $6,990 | 10 | $79 | 40 | $3 | 5 | 500 |

One account means one human, including the organization administrator. A digital employee is a separate OpenClaw agent. The organization fee includes the tier's included agents and human accounts; the monthly quote adds each agent beyond the included agents at the tier's additional-agent price and each account beyond the included accounts at the additional-account price. For example, 7 Pro accounts with 4 agents cost $79 + (2 x $5) + (2 x $35) = $159/month; 20 Max accounts with 8 agents cost $249 + (5 x $4) + (3 x $49) = $416/month; 50 Ultra accounts with 25 agents cost $699 + (10 x $3) + (15 x $79) = $1,914/month. The annual price is the organization fee for a year paid upfront, ten times the monthly fee (two months free); additional agents and accounts are quoted monthly.

The engine enforces two kinds of limit. Per account, each account, organization administrators included, may own the tier's agents per account (Pro 2, Max 3, Ultra 5). Per company, a tiered organization is capped at its included accounts plus purchased extra accounts and at its included agents plus purchased extra agents (see [Company-wide caps](#company-wide-account-and-agent-caps)). There is no account ceiling: a company buys as many extra accounts as it needs. There is no web-search allowance: Tavily search stays a platform cost.

Storage and workspace-disk add-ons, the same for every tier and quoted only (unlike extra accounts and agents they have no purchase flow, and buying one would not raise any enforced limit yet):

| Add-on | Size | Price / month |
| --- | ---: | ---: |
| Managed S3 storage | +10 GiB (10737418240 bytes) | $5 |
| Workspace disk | +5 GiB (5368709120 bytes) | $3 |

The additional-account price, the included agents, the additional-agent price and the annual price are the shared numeric contract enums `OrganizationAdditionalAccountMonthlyUsd` (Pro 5, Max 4, Ultra 3), `OrganizationIncludedAgents` (Pro 2, Max 5, Ultra 10), `OrganizationAdditionalAgentMonthlyUsd` (Pro 35, Max 49, Ultra 79) and `OrganizationAnnualPriceUsd` (Pro 790, Max 2490, Ultra 6990) in `app/records/organization_plan.py`; the catalog references the members and plan snapshots must hold exactly these values. The organization fee (`monthly_price_usd`) and the add-on fields (`storage_addon_bytes`, `storage_addon_monthly_usd`, `workspace_disk_addon_bytes`, `workspace_disk_addon_monthly_usd`) are plain integers set in the catalog (`app/services/organization_plans.py`), as are `included_accounts` (Pro 5, Max 15, Ultra 40). There is no maximum-accounts field. The engine enforces the company-wide caps, prices extra accounts and agents, and reviews the bank transfers that pay for them; it never charges a card.

The customer configures organization model providers and pays their inference charges separately. Third-party paid tools and services are also separate. The platform price does not include model tokens, promise unlimited work, or promise dedicated hardware, uptime or support service levels.

## Benchmarks and rationale

Official pricing pages accessed on the research date:

| Product | Observed billing model | Implication for MaraClaw |
| --- | --- | --- |
| [Lindy](https://www.lindy.ai/pricing) | Starts at $29.99 per active user/month with 3,000 credits per seat pooled across the workspace. Credit top-ups cost $10 per 1,000. | Seats make collaboration easy to quote, but bundled usage is a different offer from MaraClaw's customer-paid inference. |
| [Dust](https://dust.tt/home/pricing) | Business Pro seats are EUR 30/month, or EUR 24/month billed yearly, with 8,000 monthly credits. Max seats are EUR 150/month, or EUR 120/month billed yearly, with 40,000 credits. | Explicit monthly versus annual terms prevent misleading comparisons. Higher usage costs more; no unlimited-compute claim is needed. |
| [n8n](https://n8n.io/pricing) | Hosted Pro is EUR 50/month billed annually, with 10,000 monthly workflow executions and unlimited users. | An organization capacity fee is a valid alternative to pure per-seat pricing; collaborators and execution capacity need not share one meter. |

These products have different usage units, included inference, hosting models and currencies. No currency conversion or claim of equivalent capability is made. Competitor prices are observations; all MaraClaw prices and allocations above are product recommendations.

An organization base fee pays for platform operation and its included agents. Agents are priced per agent because each one is an always-on isolated guest; human accounts stay cheap so teams can collaborate without paying for compute they do not add. The larger tiers bundle more agents and human accounts, a higher per-account agent cap and a lower additional-agent price per GiB of guest memory.

The margin estimates in [aws-deployment-pricing.md](aws-deployment-pricing.md) use list prices and estimated overheads, not measured production costs; measure guest CPU, memory, storage, network, web search and support costs before activating paid organizations. Model-provider charges remain separate in every tier. No automatic overage charge, card or other online payment collection, annual-billing flow or storage / workspace-disk add-on purchase flow exists; extra accounts and agents are bought by manual bank transfer as described below.

## Admission contract

The public landing page collects the organization name, a contact email and a desired tier. Submission creates a pending organization request for the active genesis platform administrator. It does not verify the email, claim a domain, create a tenant or administrator, accept payment, or activate a subscription. A duplicate pending request receives the same public acknowledgement and retains its original name, email and tier.

Rejection records the administrator's decision and optional note; nothing else happens. Approval means commercial review has passed and provisions the organization: one transaction creates the tenant named after the request and its genesis organization administrator for the request email (email-verified, claiming the email's domain as the default domain exactly as company creation does, with a generated one-time password that must be changed at first sign-in). An email that already has an account, a domain another company already claimed, or a legacy request without an organization name cannot be approved (409, nothing created, request stays pending); an existing account is never reset or taken over. The initial credentials reach the contact only by email, sent after commit over the platform SMTP, and are retried durably; the administrator console shows pending, sent or failed delivery (SMTP acceptance is not an inbox guarantee) and can re-queue a failed delivery without approving again or changing the password. If the administrator changes the password first, the original credentials are not sent. The stored quote is a snapshot so later catalog changes do not silently change submitted terms.

The account allocations and prices are commercial terms; this request flow collects no payment, and the company-wide caps and extra-account purchases apply only after the organization exists. Existing member invitations and company membership admissions retain their own lifecycle.

## Per-account agent cap

An organization is tier-governed when an approved admission request has `provisioned_tenant_id` equal to it; the request's `tier` is the authority and is read fresh inside the creating or transferring transaction. No new column or migration exists. An organization with no such link keeps the earlier rules: the stored per-user `quota_max_agents`, with platform and organization administrators exempt, counting the creator's non-expired agents in their own organization (system agents included). Two approved requests linked to one organization are a configuration error, and the operation fails instead of falling back.

In a tier-governed organization the cap is the current catalog value for the tier (Pro 2, Max 3, Ultra 5) for every role, platform and organization administrators included, and it replaces the stored `quota_max_agents` (an inherited 2 never lowers Max or Ultra). The count is the creator's non-expired, non-system agents whose `tenant_id` is that organization, whatever their lifecycle status (stopped, failed setup and pending deletion all count). It is enforced under the shared tenant and user locks by:

- agent creation (`agent_provisioning`; the onboarding assistant is created by web-e through `POST /api/agents/`), against the explicit destination organization, so a platform administrator creating into a tiered organization is capped there only;
- ownership transfer and creator handover (`agent_ownership_transfer`), where the whole incoming batch must fit and refusal is `409 target_quota_exceeded`;
- paid company transfer disposition, where the full incoming ordinary batch is checked against the destination before any revocation or migration write and refusal is `409 transfer_destination_quota_exceeded`. Free transfers seal agents in the source and are never capped; untiered destinations keep their earlier behavior.

Agents already over the cap are never touched, further ownership is refused, and an incoming batch that adds no counted agents (only expired ones) is still accepted. Seeders and bootstrap agents are unchanged. The cap follows the tier: `OrganizationAgentLimit` (`app/records/organization_plan.py`; Pro 2, Max 3, Ultra 5, exported to the frontends as a numeric contract enum) is the only place the numbers live, and enforcement uses the catalog value for the linked tier. Stored plan snapshots must have the current shape; any other shape is refused when read, because a fresh deployment never stores one and no compatibility parser exists. `GET /api/organization-plans` lists the same shape. `AdminUserSummary.effective_max_agents` reports the limit that applies to a user (null for an uncapped legacy administrator); `quota_max_agents` is the stored legacy value and is not applied in a tier-governed organization.

## Company-wide account and agent caps

A tier-governed organization is capped as a whole (`app/services/company_capacity.py`), on top of the per-account agent cap above:

| Cap | Value |
| --- | --- |
| Active accounts | the plan's `included_accounts` (Pro 5, Max 15, Ultra 40) plus the company's effective extra accounts |
| Ordinary agents | the plan's `included_agents` (Pro 2, Max 5, Ultra 10) plus the company's effective extra agents |

Effective extras are read from `capacity_ledger.effective_extras` for the current Vietnam calendar date. There is no account ceiling. The system organizations (MaraClaw, KABERs) and organizations without a tier are never capped: `is_system` is read before the tier, so KABERs, the default organization of new end users, is unaffected.

An account is an active membership of the company (`users.is_active`) whose role is not `platform_admin`: members, agent administrators and organization administrators all count. IM channel and Feishu users are not a separate case: `channel_user_service` refuses to create a user in any organization that is neither system nor the default end-user organization (every tier-governed one), and `FeishuService.login_or_register` has no caller. An agent is counted as the per-account cap counts it (non-expired, non-system, any lifecycle status), company-wide.

Existing members and agents are never removed or deactivated; only a net increase is refused. Every gate runs under the tenant row lock its writer already holds (or takes first), so two concurrent writers cannot both take the last slot.

- **Accounts.** The refusal is `409` with `{"code": "company_account_limit_reached", "message", "limit", "used"}` (`CompanyAccountLimitReachedError`, one application handler in `app/main.py`). Gates: `org_membership._set_user_org` (joining with an invitation, joining the suggested or default organization, transfer, admission approval and automatic invitation admission, so a refusal rolls back the whole admission and any agent disposition), `registration_service.create_user_with_identity` for an active user, and `admin_provisioning` for `create_additional_org_admin`, `set_end_user_active` (activation) and `set_peer_admin_active` (organization administrator reactivation). Verifying an email (the link, or password login when no SMTP is configured) always verifies the identity, but `org_membership.activate_verified_memberships` reactivates only the inactive memberships that fit their company's cap (the member is not counted against itself); a membership that does not fit stays inactive and verification still succeeds. `tenant_provisioning.attach_genesis_org_admin` (genesis admin repair, `POST /api/tenants/{id}/genesis-org-admin`) takes an account and is refused before anything is written; the startup seed of the KABERs genesis admin is a system organization and uncapped. `POST /api/enterprise/invite-users` and `POST /api/enterprise/invitation-codes` refuse with the same `409` when the company has no free account (advisory: accepting an invitation still hits the gate). Re-enabling a disabled company (`tenant_lifecycle`) restores its members only up to the company's free account slots (`company_capacity.free_account_slots`, read under the tenant lock): organization admins first (the genesis admin first among them), then the most recently updated membership, then the oldest account; the rest stay inactive for an org admin to activate as slots free up (`UserDAO.reactivate_for_tenant`).
- **Agents.** `quota_guard.check_agent_creation_quota` checks the company cap after the per-account cap: agent creation, and renewal of an expired agent through `check_agent_renewal_quota`, are refused with `403` `Your company has used all N agents. An org admin can buy extra agents on the Users page in the admin console.` (`quota_type="company_max_agents"`). Paid company transfer disposition checks the whole incoming ordinary batch against the destination company after the applicant's per-account check and refuses with `409 transfer_destination_quota_exceeded`. Ownership transfer and creator handover stay inside one company and do not change its count, so they are not checked against it.

## Buying extra accounts and agents

A tiered, non-system organization raises its company-wide caps by buying extra accounts and extra agents (`app/services/capacity_orders.py`, endpoints in [admin-apis.md](admin-apis.md#b12-capacity-purchase-workflow---apicapacity)). Payment is a manual bank transfer; nothing is charged online.

1. **Order.** An organization admin enters the extra accounts and extra agents (whole numbers 0..10 000 each, at least one in total) on the Users page of the admin console. `GET /api/capacity/overview` gives that page the tier prices, the Vietnam date, the remaining days, the USD/VND rate and the company's caps and usage, so it previews the same amount the server computes. `POST /api/capacity/orders` prices the order on the server and opens it as `awaiting_payment`. The client never sends a price.
2. **Pay.** The order carries the bank recipient (bank name, account number, branch name), the VND amount and a short `reference` (`MC` plus 8 uppercase base32 characters) to write in the transfer note. The quote, the tier unit prices, the USD/VND rate and the recipient are copied onto the order when it is created and never change afterwards, whatever the genesis platform admin later saves.
3. **Finish.** The organization admin attaches evidence of the transfer (PNG, JPEG, WebP or PDF up to 10 MiB, recognised by its leading bytes) with `POST /api/capacity/orders/{id}/evidence`. The order becomes `submitted` and every active platform admin gets an in-app notification.
4. **Review.** Any active platform admin approves or rejects (a note is required to reject). The submitter and the company's active org admins are notified. Only an approved order raises the company's effective extras, and from then on `company_caps` and every gate in [Company-wide account and agent caps](#company-wide-account-and-agent-caps) see the higher limits. A purchase can be cancelled by an org admin while it still awaits payment; a company has at most one such open purchase at a time.

### Pricing

The calendar is Vietnam's (`Asia/Ho_Chi_Minh`), and today counts as a charged day: with `N` days in the month and today the day `d`, `N - d + 1` days remain.

- `monthly_usd = extra_accounts x additional_account_monthly_usd + extra_agents x additional_agent_monthly_usd`, at the company's tier (Pro 5 / 35, Max 4 / 49, Ultra 3 / 79).
- A purchase costs `usd_exact = monthly_usd x remaining / N`, kept as an exact fraction.
- `amount_usd_cents` is `usd_exact x 100` rounded half up.
- `amount_vnd = ceil(usd_exact x usd_vnd_rate / 1000) x 1000`: the VND amount comes from the exact USD value, not from the rounded cents, and is always a whole number of thousands.
- `usd_vnd_rate` is a whole number of VND per USD saved by the genesis platform admin.
- A renewal is never prorated: it is a whole month (`remaining = N`), so it equals a purchase made on the 1st.

Example: on November 14 (30 days, 17 remaining) 2 extra accounts and 3 extra agents on Pro are $115 a month, so $115 x 17 / 30 = $65.1667, 6517 cents, and 1 630 000 VND at 25 000 VND per USD. The pure arithmetic is `app/services/capacity_pricing.py`; money is `Fraction` and integers, never a float.

### Effective extras and renewal

Orders have a `kind` (`purchase` or `renewal`), a `billing_month` (the first of a Vietnam month), their quantities and one of the statuses `awaiting_payment`, `submitted`, `approved`, `rejected`, `cancelled`, `expired`. `capacity_ledger.effective_extras(d)`, with `M` the month of `d`, sums the company's orders billed for `M`:

- every approved purchase;
- every renewal that is approved or submitted, and every awaiting-payment renewal while `d <= grace_until`;
- plus, while no primary renewal for `M` exists yet (the worker has not run, or payments are not configured), the approved coverage of the previous month, so a company is never penalized before it was asked to pay.

`coverage(M)` is the sum of the approved orders (purchases and renewals) billed for `M`. Rejected, expired and cancelled orders count nothing, and a submitted purchase counts only once it is approved.

- **Renewal on the 1st.** The `capacity_renewal` worker loop (`app/services/capacity_renewal_worker.py`, worker role, every `CAPACITY_RENEWAL_INTERVAL_SECONDS`, default 3600) creates, for each company whose coverage of the previous month is not empty, one primary renewal for the current Vietnam month: the same quantities at the company's current tier prices, the whole month, the current rate and recipient. It runs under the tenant row lock and is idempotent (a partial unique index allows one primary renewal per company and month), and org admins are notified (`capacity_renewal_due`, link `/capacity-orders/{id}`). Without configured payment settings it skips the company with a warning and retries on the next pass.
- **Grace.** A renewal carries `grace_until`. Grace is 7 days and the start day counts as the first, so an unpaid renewal counts through the 7th and lapses on the 8th: for the primary renewal `grace_until` is the later of the 7th of its month (the 1st plus 6 days) and its creation date plus 6 days, so a renewal generated late still gives a full week. From the lapse on, new invitations and agents are refused again, and nothing is removed. A paid (approved or submitted) renewal counts for the whole month.
- **Late approval.** Approving any order billed for an earlier month also inserts a supplementary renewal for the current month with the same quantities (`source_order_id` = the approved order, one per source), a whole month at current prices, awaiting payment, `grace_until` = creation date + 6 days (seven days counting the creation day), and notifies the org admins. The one exception is an order billed for the previous month while the current month's primary renewal does not exist yet: nothing extra is made, because the ledger fallback and generation both include it through the previous month's coverage. An order billed earlier than that is outside that coverage, so it is renewed whether or not the primary renewal exists (a purchase billed in October, paid on the 30th and approved on December 2 gets its December renewal at once, and its extras count meanwhile).
- **Expiry.** Every pass expires `awaiting_payment` orders billed for an earlier month: a purchase unpaid by the end of its month, or a renewal unpaid by the end of its month. A submitted order is never auto-expired, so evidence already sent can still be reviewed. The company then buys again at a prorated price; unpaid extras are not renewed.
- **Known gap.** The previous month's coverage stands in for one month only. If payment settings stay unconfigured for a whole month, the extras it covered are not renewed and vanish the month after.

Concurrency: order creation and decisions serialize with the worker on `SELECT ... FROM tenants WHERE id = ... FOR UPDATE`; every status change is a `SELECT ... FOR UPDATE` on the order followed by an `UPDATE` guarded by `AND status = <expected>`; one awaiting-payment purchase per company, one primary renewal per company and month and one renewal per source order are partial unique indexes. The table is `capacity_orders` in `scripts/schema_baseline.sql`, a fresh-baseline table with no upgrade patch.

### Payment settings

Bank name, account number, branch name and `usd_vnd_rate` are one `system_settings` row (`capacity_payment_settings`). Only the genesis platform admin reads or writes them (`GET`/`PUT /api/admin/capacity-payment-settings`; each change is audited with the fields that changed); the generic enterprise system-settings endpoints answer 404 for the key. Until they are saved, `POST /api/capacity/orders` answers `409 payments_not_configured` and the worker skips renewals. Evidence files are stored under `_capacity_payments/{tenant_id}/{order_id}/` through the storage backend, outside every agent prefix and so outside the managed storage quota.

## Per-agent daily messages

Each plan carries `daily_messages_per_agent`, the human messages one agent accepts per local day. `OrganizationDailyAgentMessageLimit` (`app/records/organization_plan.py`; Pro 100, Max 200, Ultra 500, exported to the frontends as a numeric contract enum) is the only place the numbers live; the catalog references the members and enforcement reads `plan_for(tier).daily_messages_per_agent`. Like the rest of the plan, a stored snapshot without the field, or with a value outside the enum, is refused when read; there is no default or compatibility parser. The cap counts accepted messages, not tokens or model cost: organizations pay for their own LLM inference through their own model providers, which is why the platform fee covers hosting rather than inference. In a tier-governed organization the per-user `quota_message_limit` is not applied (`charge_conversation_quota` returns without charging, as the stored `quota_max_agents` is not applied for agent counts), so this per-agent cap is the only message limit there; organizations without a tier keep the per-user limit. The agent `max_llm_calls_per_day` stays a separate limit.

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
| Pro | 2147483648 (2 GiB) | 26214400 (25 MiB) |
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
| Pro | 2 GiB | 1.5 | 1024 | 512 | 3 GiB |
| Max | 3 GiB | 2.0 | 1536 | 768 | 5 GiB |
| Ultra | 5 GiB | 3.0 | 2048 | 1024 | 8 GiB |

Like every other plan field, a stored snapshot without these five fields, or with a value outside the enum, is refused when read: deployments start from a blank database, so there is no fill-in or compatibility parser. The guest limits apply when a guest is created and when a stopped guest resumes; workspace disk is a measured soft quota (host workspace directory plus the guest's writable layer) that refuses new human work while exceeded. It is separate from `storage_limit_bytes`, which counts managed S3 objects only. Enforcement details, system-agent and untiered fallbacks and the resource-pressure records used for upgrade suggestions: [guest-containers.md](guest-containers.md).

The repository initializes database objects from `engine/scripts/schema_baseline.sql`. An existing `organization_admission_requests` table created before named provisioning is upgraded in place on the next bootstrap (rows kept, organization name empty, never provisioned retroactively); no other existing-database migration is provided.
