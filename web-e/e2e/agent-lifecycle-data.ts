import type { AgentLifecycle } from '../src/lib/agent-lifecycle-api'

export const lifecycleReady: AgentLifecycle = {
  as_of: '2030-01-01T00:00:00Z', runtime: { recorded_status: 'stopped', recorded_container_id: null },
  setup: { setup_pending: false, state: null, failure_code: null, retryable: false },
  start: null, optional_mcp: [], upgrade: { notice: null, run: null, required: false, can_upgrade: false },
  availability: { policy_eligible: true, blocking_codes: ['runtime_stopped'], can_start: true, can_retry_setup: true, can_retry_mcp: true },
}
export const warningLifecycle: AgentLifecycle = {
  ...lifecycleReady,
  optional_mcp: [
    { server_id: 'company/research', state: 'warning', warning_code: 'connection_failed', attempts: 2, retryable: true },
    { server_id: 'company/working', state: 'succeeded', warning_code: null, attempts: 1, retryable: false },
  ],
}
