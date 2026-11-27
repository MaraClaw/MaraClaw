export const companyId = '11111111-1111-4111-8111-111111111111'
export const agentId = '22222222-2222-4222-8222-222222222222'
export const runtimePage = {
  company_id: companyId, as_of: '2026-11-15T04:00:00Z', observation_available: true, next_cursor: null,
  items: [{
    agent_id: agentId, name: 'Synthetic Agent', creator_id: companyId, recorded_status: 'stopped',
    recorded_container_id: 'a'.repeat(64), runtime_loss: null, start: null, upgrade_in_progress: false,
    workspace: { used_bytes: 0, limit_bytes: 1024 ** 3, measured_at: '2026-11-15T03:00:00Z', over_since: null, walk_failed_at: null, remeasure_requested_at: null },
    blocking_codes: [], observation: { state: 'observed', container_status: 'exited', running: false,
      started_at: '2026-11-15T02:00:00Z', finished_at: '2026-11-15T03:00:00Z', exit_code: 0,
      restart_count_since_last_start: 0, oom_killed_since_last_start: false },
  }],
} satisfies CompanyRuntimePageOut
export const tierPreview = {
  tenant_id: companyId, current_tier: 'max', target_tier: 'pro',
  accounts: { used: 7, included: 2, extras: 1, limit: 3, over_included: 5, over_limit: 4 },
  agents: { used: 9, included: 5, extras: 1, limit: 6, over_included: 4, over_limit: 3 },
  per_account_agent_limit: 2,
  owners_over_limit: [{ user_id: agentId, display_name: 'Synthetic Owner', agents_count: 4, limit: 2, overage: 2 }],
  running_guests: [{ agent_id: agentId, name: 'Synthetic Agent', container_id: 'b'.repeat(64) }],
  guest_observation_available: true,
  guest_limits_apply_on: 'stop_resume_or_replacement', existing_orders_keep_recorded_tier: true,
  next_capacity_renewal: { billing_month: '2026-12-01', extra_accounts: 1, extra_agents: 1, account_unit_usd: 2, agent_unit_usd: 3, monthly_usd: 5 },
} satisfies CompanyTierPreview
import type { CompanyRuntimePageOut, CompanyTierPreview } from '../src/lib/engine-contracts.generated.ts'
