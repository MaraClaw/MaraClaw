import { matchPath } from 'react-router-dom'

import type { AgentUpdate, OpenClawRun } from './openclaw-updates-api.ts'

export const openclawUpdateRoute = 'updates/:noticeId'

export function updateNoticePath(noticeId: string) {
  return `/app/updates/${encodeURIComponent(noticeId)}`
}

export function agentSettingsPath(agentId: string) {
  return `/app/agents/${encodeURIComponent(agentId)}/settings`
}

export function isActiveUpdate(run: OpenClawRun | null | undefined): boolean {
  if (!run) return false
  switch (run.state) {
    case 'pending':
    case 'cutover': return true
    case 'succeeded':
    case 'failed': return false
    default: return assertNever(run.state)
  }
}

export type UpdateGate = 'checking' | 'unavailable' | 'upgrading' | 'required' | 'open'

export function updateWorkGate(input: {
  readonly update: AgentUpdate | undefined
  readonly error: boolean
  readonly submitting: boolean
}): UpdateGate {
  if (input.submitting || isActiveUpdate(input.update?.run)) return 'upgrading'
  if (input.error) return 'unavailable'
  if (!input.update) return 'checking'
  return input.update.required ? 'required' : 'open'
}

export function canMountAgentOutlet(pathname: string, gate: UpdateGate): boolean {
  return Boolean(matchPath('/app/agents/:agentId/settings', pathname)) || gate === 'open'
}

export function assertNever(value: never): never {
  throw new Error(`Unexpected OpenClaw update state: ${String(value)}`)
}
