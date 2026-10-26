import type { AgentLifecycle, OptionalMCP } from '../src/lib/agent-lifecycle-api.ts'

export const mcpWarning: OptionalMCP = {
  server_id: 'company/research', state: 'warning', warning_code: 'connection_failed', attempts: 2, retryable: true,
}
export const lifecycle: AgentLifecycle = {
  as_of: '2030-01-01T00:00:00Z',
  runtime: { recorded_status: 'stopped', recorded_container_id: null },
  setup: { setup_pending: false, state: 'succeeded', failure_code: null, retryable: false },
  start: { operation_id: '33333333-3333-4333-8333-333333333333', state: 'pending', attempts: 2,
    last_attempt_at: '2029-12-31T23:59:40Z', next_retry_at: '2030-01-01T00:00:20Z', last_failure_code: 'capacity_exceeded' },
  optional_mcp: [mcpWarning],
  upgrade: { notice: null, run: null, required: false, can_upgrade: false },
  availability: { policy_eligible: true, blocking_codes: ['runtime_stopped'], can_start: true, can_retry_setup: false, can_retry_mcp: true },
}
