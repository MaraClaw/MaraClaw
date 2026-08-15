import { apiRequest } from '@/lib/http'

export type ControlResult = {
  status: string
  detail?: string
  url?: string
  screenshot?: string | null
  screen_size?: { width?: number; height?: number } | null
  locked_by?: string
  cookies_exported?: boolean
  cookie_count?: number
}

export async function controlLock(
  agentId: string,
  sessionId: string,
  envType = 'browser',
): Promise<ControlResult> {
  return apiRequest(`/api/agents/${agentId}/control/lock`, {
    method: 'POST',
    body: { session_id: sessionId, env_type: envType },
  })
}

export async function controlUnlock(
  agentId: string,
  sessionId: string,
  opts?: { export_cookies?: boolean; platform_hint?: string },
): Promise<ControlResult> {
  return apiRequest(`/api/agents/${agentId}/control/unlock`, {
    method: 'POST',
    body: { session_id: sessionId, export_cookies: opts?.export_cookies ?? true, platform_hint: opts?.platform_hint },
  })
}

export async function controlScreenshot(agentId: string, sessionId: string): Promise<ControlResult> {
  return apiRequest(`/api/agents/${agentId}/control/screenshot`, {
    method: 'POST',
    body: { session_id: sessionId },
  })
}

export async function controlCurrentUrl(agentId: string, sessionId: string): Promise<ControlResult> {
  return apiRequest(`/api/agents/${agentId}/control/current-url`, {
    method: 'POST',
    body: { session_id: sessionId },
  })
}

export async function controlClick(
  agentId: string,
  sessionId: string,
  x: number,
  y: number,
  button = 'left',
): Promise<ControlResult> {
  return apiRequest(`/api/agents/${agentId}/control/click`, {
    method: 'POST',
    body: { session_id: sessionId, x, y, button },
  })
}

export async function controlType(agentId: string, sessionId: string, text: string): Promise<ControlResult> {
  return apiRequest(`/api/agents/${agentId}/control/type`, {
    method: 'POST',
    body: { session_id: sessionId, text },
  })
}

export async function controlPressKeys(agentId: string, sessionId: string, keys: string[]): Promise<ControlResult> {
  return apiRequest(`/api/agents/${agentId}/control/press_keys`, {
    method: 'POST',
    body: { session_id: sessionId, keys },
  })
}

export async function controlDrag(
  agentId: string,
  sessionId: string,
  from: { x: number; y: number },
  to: { x: number; y: number },
): Promise<ControlResult> {
  return apiRequest(`/api/agents/${agentId}/control/drag`, {
    method: 'POST',
    body: {
      session_id: sessionId,
      from_x: from.x,
      from_y: from.y,
      to_x: to.x,
      to_y: to.y,
    },
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
