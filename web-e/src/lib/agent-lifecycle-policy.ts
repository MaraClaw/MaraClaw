import type { AgentLifecycle, OptionalMCP } from './agent-lifecycle-api.ts'

export const recordedStatus = {
  creating: 'Creating', running: 'Running', idle: 'Idle', stopped: 'Stopped', error: 'Error',
} as const satisfies Record<AgentLifecycle['runtime']['recorded_status'], string>

export const lifecycleBlockMessage = {
  expired: 'This agent has expired. Contact your company admin to extend its access.',
  owner_inactive: 'The creator account is inactive. Contact your company admin.',
  identity_inactive: 'The creator identity is inactive. Contact your company admin.',
  tenant_inactive: 'The company is inactive. Contact your company admin.',
  owner_tenant_changed: 'The creator changed companies. Contact your company admin.',
  transfer_pending: 'A company transfer is pending. Wait for it to finish.',
  setup_pending: 'Required setup must finish before starting.',
  runtime_stopped: 'The recorded runtime is stopped.',
  runtime_not_running: 'The recorded runtime is not running.',
  upgrade_in_progress: 'An upgrade is in progress. Wait for it to finish.',
  upgrade_required: 'An upgrade is required before continuing work.',
} as const satisfies Record<AgentLifecycle['availability']['blocking_codes'][number], string>

export const startFailureMessage = {
  capacity_exceeded: 'There is not enough runtime capacity. The start request will retry when scheduled.',
  configuration_failed: 'Runtime configuration needs attention. Contact your company admin.',
  readiness_failed: 'The runtime did not become ready. Check the next retry or contact your company admin.',
  ownership_conflict: 'Runtime ownership needs attention. Contact your company admin.',
  runtime_failed: 'The runtime could not start. Check the next retry or contact your company admin.',
  publication_failed: 'The start result could not be recorded. Contact your company admin if it persists.',
  upgrade_in_progress: 'Start is waiting for the upgrade to finish.',
} as const satisfies Record<NonNullable<NonNullable<AgentLifecycle['start']>['last_failure_code']>, string>

export const mcpWarningMessage = {
  missing_credentials: 'Saved credentials are missing. Update the integration connection before retrying.',
  authorization_required: 'Authorization is required. Reconnect this integration before retrying.',
  unsupported_remote_transport: 'This connection type is not supported. Contact your company admin.',
  connection_failed: 'The integration could not connect. Check its saved connection and retry.',
  tools_unverified: 'The imported tools could not be verified. Retry or contact your company admin.',
  import_failed: 'The integration could not be imported. Retry or contact your company admin.',
} as const satisfies Record<NonNullable<OptionalMCP['warning_code']>, string>

export function isAgentExpired(agent: { readonly is_expired?: boolean; readonly expires_at?: string | null }, now = Date.now()) {
  return Boolean(agent.is_expired || (agent.expires_at && Date.parse(agent.expires_at) <= now))
}

export function canRequestAgentDeletion(agent: {
  readonly creator_id: string; readonly is_system?: boolean; readonly access_level?: string | null
}, user: { readonly id: string; readonly role: string } | null) {
  return !agent.is_system && Boolean(user && (user.id === agent.creator_id ||
    (user.role === 'org_admin' && agent.access_level === 'manage')))
}
