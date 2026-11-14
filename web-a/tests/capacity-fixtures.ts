import { capacityOrderIdSchema, capacityOrderSchema, capacityOverviewSchema } from '../src/lib/capacity-api.ts'

export const capacityTenantId = 'c2222222-2222-4222-8222-222222222222'
export const capacityOrderId = capacityOrderIdSchema.parse('b1111111-1111-4111-8111-111111111111')
export const capacityUserId = 'd3333333-3333-4333-8333-333333333333'

export const purchaseOrder = capacityOrderSchema.parse({
  id: capacityOrderId, tenant_id: capacityTenantId, tenant_name: 'Acme Co', reference: 'MC7K3QX2ZA',
  kind: 'purchase', status: 'awaiting_payment', source_order_id: null, tier: 'pro', billing_month: '2026-11-01',
  extra_accounts: 2, extra_agents: 3, account_unit_usd: 5, agent_unit_usd: 35, days_charged: 17, days_in_month: 30,
  amount_usd_cents: 6517, usd_vnd_rate: 25000, amount_vnd: 1_630_000,
  recipient: { bank_name: 'Example Bank', account_number: '0123456789', branch_name: 'District 1 Branch' },
  grace_until: null, evidence: null, created_by: capacityUserId, created_at: '2026-11-14T03:00:00Z',
  submitted_by: null, submitted_at: null, reviewed_by: null, reviewed_at: null, review_note: null,
})

export const submittedOrder = capacityOrderSchema.parse({
  ...purchaseOrder, status: 'submitted', submitted_by: capacityUserId, submitted_at: '2026-11-14T04:00:00Z',
  evidence: { content_type: 'image/png', filename: 'receipt.png', size_bytes: 20480, sha256: 'a'.repeat(64) },
})

export const renewalOrder = capacityOrderSchema.parse({
  ...purchaseOrder, id: 'e4444444-4444-4444-8444-444444444444', kind: 'renewal', billing_month: '2026-12-01',
  days_charged: 31, days_in_month: 31, amount_usd_cents: 11500, amount_vnd: 2_875_000, grace_until: '2026-12-07',
})

export const cappedOverview = capacityOverviewSchema.parse({
  tenant_id: capacityTenantId, capped: true, tier: 'pro', included_accounts: 10, included_agents: 3,
  extra_accounts: 2, extra_agents: 1, account_limit: 12, agent_limit: 4, accounts_used: 11, agents_used: 4,
  account_unit_usd: 5, agent_unit_usd: 35, today: '2026-11-14', days_in_month: 30, remaining_days: 17,
  payments_configured: true, usd_vnd_rate: 25000, open_orders: [], current_renewal: null,
})

export const uncappedOverview = capacityOverviewSchema.parse({
  ...cappedOverview, capped: false, tier: null, included_accounts: null, included_agents: null,
  extra_accounts: 0, extra_agents: 0, account_limit: null, agent_limit: null, account_unit_usd: null,
  agent_unit_usd: null, payments_configured: false, usd_vnd_rate: null,
})
