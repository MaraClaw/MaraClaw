import { z } from 'zod'

import { apiRequest } from './http.ts'
import { ControlActionResultSchema, ControlEnvironmentSchema, ControlStatusSchema } from './engine-contracts.generated.ts'

export type ControlLease = {
  readonly agentId: string
  readonly sessionId: string
  readonly ownerToken: string
}

const lockResult = z.discriminatedUnion('status', [
  z.object({ status: z.literal('locked'), owner_token: z.string().min(1) }),
  z.object({ status: z.literal('already_locked') }),
])

const controlResultSchema = ControlActionResultSchema.extend({
  status: ControlStatusSchema,
  screen_size: z.object({ width: z.number().optional(), height: z.number().optional() }).nullable().optional(),
})
export type ControlResult = z.infer<typeof controlResultSchema>

export async function controlLock(
  agentId: string,
  sessionId: string,
  envType = 'browser',
): Promise<z.infer<typeof lockResult>> {
  return apiRequest(`/api/agents/${agentId}/control/lock`, {
    schema: lockResult, method: 'POST',
    body: { session_id: sessionId, env_type: ControlEnvironmentSchema.parse(envType) },
  })
}

async function ownerRequest(lease: ControlLease, endpoint: string, body: object = {}): Promise<ControlResult> {
  return apiRequest(`/api/agents/${lease.agentId}/control/${endpoint}`, {
    schema: controlResultSchema, method: 'POST',
    body: { ...body, session_id: lease.sessionId, owner_token: lease.ownerToken },
  })
}

export function controlUnlock(
  lease: ControlLease,
  opts?: { export_cookies?: boolean; platform_hint?: string },
): Promise<ControlResult> {
  return ownerRequest(lease, 'unlock', {
    export_cookies: opts?.export_cookies ?? true, platform_hint: opts?.platform_hint,
  })
}

export function controlScreenshot(lease: ControlLease): Promise<ControlResult> {
  return ownerRequest(lease, 'screenshot')
}

export function controlCurrentUrl(lease: ControlLease): Promise<ControlResult> {
  return ownerRequest(lease, 'current-url')
}

export function controlClick(lease: ControlLease, point: { x: number; y: number }, button = 'left'): Promise<ControlResult> {
  return ownerRequest(lease, 'click', { ...point, button })
}

export function controlType(lease: ControlLease, text: string): Promise<ControlResult> {
  return ownerRequest(lease, 'type', { text })
}

export function controlPressKeys(lease: ControlLease, keys: readonly string[]): Promise<ControlResult> {
  return ownerRequest(lease, 'press_keys', { keys })
}

export function controlDrag(
  lease: ControlLease,
  from: { x: number; y: number },
  to: { x: number; y: number },
): Promise<ControlResult> {
  return ownerRequest(lease, 'drag', {
    from_x: from.x, from_y: from.y, to_x: to.x, to_y: to.y,
  })
}

export type AgentCredential = {
  id: string
  agent_id: string
  credential_type: string
  platform: string
  display_name: string
  status: string
  has_cookies: boolean
  cookies_updated_at?: string | null
}

export async function listCredentials(agentId: string): Promise<AgentCredential[]> {
  return apiRequest(`/api/agents/${agentId}/credentials/`)
}

export async function createCredential(
  agentId: string,
  body: { platform: string; display_name?: string; cookies_json?: string; credential_type?: string },
): Promise<AgentCredential> {
  return apiRequest(`/api/agents/${agentId}/credentials/`, { method: 'POST', body })
}

export async function deleteCredential(agentId: string, credentialId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/credentials/${credentialId}`, { method: 'DELETE' })
}

export type GogcliStatus = {
  authenticated: boolean
  account_hint?: string | null
  detail: string
}

export async function setGogcliKeyring(agentId: string, password: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/gogcli/keyring-secret`, { method: 'POST', body: { password } })
}

export async function startGogcliAuth(agentId: string, accountEmail: string): Promise<{ auth_url: string; detail: string }> {
  return apiRequest(`/api/agents/${agentId}/gogcli/auth/start`, { method: 'POST', body: { account_email: accountEmail } })
}

export async function getGogcliStatus(agentId: string): Promise<GogcliStatus> {
  return apiRequest(`/api/agents/${agentId}/gogcli/auth/status`)
}

export type PublishedPage = {
  id: string
  short_id: string
  source_path: string
  title?: string | null
  view_count: number
  created_at?: string | null
  url: string
}

export async function listPublishedPages(agentId: string): Promise<PublishedPage[]> {
  return apiRequest(`/api/pages/list?agent_id=${agentId}`)
}

export async function publishWorkspacePage(agentId: string, path: string): Promise<PublishedPage> {
  return apiRequest('/api/pages/', { method: 'POST', body: { agent_id: agentId, path } })
}

export async function unpublishPage(pageId: string): Promise<void> {
  await apiRequest(`/api/pages/${pageId}`, { method: 'DELETE' })
}
