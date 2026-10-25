import { apiFormRequest, apiRequest } from './http.ts'
import type { ChatMessage } from './chat/message.ts'
export type { ChatMessage } from './chat/message.ts'
export { createSchedule, deleteSchedule, listScheduleHistory, listSchedules, runSchedule, updateSchedule } from './schedules-api.ts'
export type { ScheduleOut } from './schedules-api.ts'
export { completeOnboarding, createPersonalAssistant, getOnboardingStatus, markOnboardingSkipped, ONBOARDING_SKIP_KEY, startOnboarding, wasOnboardingSkipped } from './onboarding-api.ts'
export type { OnboardingStatus, PersonalAssistantInput, PersonalAssistantResponse } from './onboarding-api.ts'
export { createAgent, deleteAgent, getAgent, getAgentPermissions, handoverAgent, listAgents, listPermissionCandidates, listTemplates, startAgent, stopAgent, updateAgent, updateAgentPermissions } from './agents-api.ts'
export type { AgentOut, AgentPermissions, AgentTemplate } from './agents-api.ts'
export { completeFocus, createTask, deleteTrigger, listFocus, listTaskLogs, listTasks, listTriggers, patchTrigger, triggerTask, upsertFocus } from './agent-work-api.ts'
export type { FocusItem, TaskOut } from './agent-work-api.ts'
export { CHANNEL_FIELDS, CHANNELS, deleteChannel, getChannel, getChannelWebhook, getWechatQr, saveChannel } from './channels-api.ts'
export type { ChannelConfig, ChannelField } from './channels-api.ts'

export type ChatSession = {
  id: string
  agent_id: string
  user_id: string
  title: string
  source_channel: string
  is_group?: boolean
  created_at: string
  last_message_at?: string | null
  message_count: number
  unread_count: number
  is_primary?: boolean
}

export type FileInfo = {
  name: string
  path: string
  is_dir: boolean
  size: number
  modified_at?: string
  version_token?: string | null
  url?: string | null
}

export type AgentTool = {
  id: string
  name: string
  display_name?: string
  description?: string
  category?: string
  enabled: boolean
  is_default?: boolean
  source?: string
}

export type SkillRow = {
  id: string
  name: string
  folder_name?: string
  description?: string
}

export async function listSessions(agentId: string, scope = 'mine'): Promise<ChatSession[]> {
  return apiRequest(`/api/agents/${agentId}/sessions?scope=${scope}`)
}

export async function createSession(agentId: string, title?: string): Promise<ChatSession> {
  return apiRequest(`/api/agents/${agentId}/sessions`, { method: 'POST', body: { title } })
}

export async function renameSession(agentId: string, sessionId: string, title: string): Promise<ChatSession> {
  return apiRequest(`/api/agents/${agentId}/sessions/${sessionId}`, { method: 'PATCH', body: { title } })
}

export async function deleteSession(agentId: string, sessionId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/sessions/${sessionId}`, { method: 'DELETE' })
}

export async function listSessionMessages(agentId: string, sessionId: string): Promise<ChatMessage[]> {
  return apiRequest(`/api/agents/${agentId}/sessions/${sessionId}/messages?limit=200`)
}

export async function uploadChatFile(file: File, agentId: string): Promise<{
  filename: string
  extracted_text: string
  workspace_path: string
}> {
  const form = new FormData()
  form.append('file', file)
  form.append('agent_id', agentId)
  return apiFormRequest('/api/chat/upload', form)
}

export async function listFiles(agentId: string, path = ''): Promise<FileInfo[]> {
  const query = path ? `?path=${encodeURIComponent(path)}` : ''
  return apiRequest(`/api/agents/${agentId}/files/${query}`)
}

export async function readFileContent(agentId: string, path: string): Promise<{ path: string; content: string; version_token?: string }> {
  return apiRequest(`/api/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`)
}

export async function writeFileContent(
  agentId: string,
  path: string,
  content: string,
  expectedVersion?: string,
): Promise<{ status: string; path: string }> {
  return apiRequest(`/api/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, {
    method: 'PUT',
    body: { content, expected_version_token: expectedVersion },
  })
}

export async function deleteFileContent(agentId: string, path: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, {
    method: 'DELETE',
  })
}

export async function uploadWorkspaceFile(agentId: string, file: File, path = 'workspace/knowledge_base'): Promise<unknown> {
  const form = new FormData()
  form.append('file', file)
  return apiFormRequest(`/api/agents/${agentId}/files/upload?path=${encodeURIComponent(path)}`, form)
}

export async function listSkills(): Promise<SkillRow[]> {
  return apiRequest('/api/skills/')
}

export async function searchClawhub(q: string): Promise<Array<{ slug: string; name?: string; description?: string }>> {
  return apiRequest(`/api/skills/clawhub/search?q=${encodeURIComponent(q)}`)
}

export async function installClawhub(slug: string): Promise<unknown> {
  return apiRequest('/api/skills/clawhub/install', { method: 'POST', body: { slug } })
}

export async function importSkillToAgent(agentId: string, skillId: string): Promise<unknown> {
  return apiRequest(`/api/agents/${agentId}/files/import-skill`, { method: 'POST', body: { skill_id: skillId } })
}

export async function listAgentTools(agentId: string): Promise<AgentTool[]> {
  return apiRequest(`/api/tools/agents/${agentId}`)
}

export async function updateAgentTools(agentId: string, updates: Array<{ tool_id: string; enabled: boolean }>): Promise<void> {
  await apiRequest(`/api/tools/agents/${agentId}`, { method: 'PUT', body: updates })
}

export async function getAgentToolConfig(agentId: string, toolId: string): Promise<{
  merged_config: Record<string, unknown>
  config_schema: Record<string, unknown>
}> {
  return apiRequest(`/api/tools/agents/${agentId}/tool-config/${toolId}`)
}

export async function saveAgentToolConfig(agentId: string, toolId: string, config: Record<string, unknown>): Promise<void> {
  await apiRequest(`/api/tools/agents/${agentId}/tool-config/${toolId}`, { method: 'PUT', body: { config } })
}

export async function lockFile(agentId: string, path: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/files/locks`, { method: 'POST', body: { path } })
}

export async function unlockFile(agentId: string, path: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/files/locks?path=${encodeURIComponent(path)}`, { method: 'DELETE' })
}

export async function browseSkills(path = ''): Promise<Array<{ name: string; path: string; is_dir: boolean; size?: number }>> {
  const query = path ? `?path=${encodeURIComponent(path)}` : ''
  return apiRequest(`/api/skills/browse/list${query}`)
}

export async function browseSkillRead(path: string): Promise<{ content: string }> {
  return apiRequest(`/api/skills/browse/read?path=${encodeURIComponent(path)}`)
}

export type HumanRelationship = {
  id: string
  member_id: string
  relation: string
  relation_label?: string
  description?: string
  member?: { name?: string; email?: string; title?: string } | null
}

export async function listRelationships(agentId: string): Promise<HumanRelationship[]> {
  return apiRequest(`/api/agents/${agentId}/relationships/`)
}

export async function saveRelationships(
  agentId: string,
  relationships: Array<{ member_id: string; relation: string; description?: string }>,
): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/relationships/`, { method: 'PUT', body: { relationships } })
}

export async function deleteRelationship(agentId: string, relId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/relationships/${relId}`, { method: 'DELETE' })
}

export async function listRelationshipCandidates(
  agentId: string,
): Promise<Array<{ id: string; name?: string; email?: string; title?: string }>> {
  return apiRequest(`/api/agents/${agentId}/relationships/member-candidates`)
}

export async function listAgentApprovals(agentId: string): Promise<
  Array<{ id: string; action_type?: string; status?: string; details?: unknown; created_at?: string }>
> {
  return apiRequest(`/api/agents/${agentId}/approvals`)
}

export async function resolveAgentApproval(agentId: string, approvalId: string, action: 'approve' | 'reject'): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/approvals/${approvalId}/resolve`, { method: 'POST', body: { action } })
}

export async function getAgentMetrics(agentId: string): Promise<{
  tokens?: { used_today?: number; used_month?: number; limit_day?: number | null }
  tasks?: { total?: number; done?: number; pending?: number }
  approvals?: { pending?: number }
  status?: string
}> {
  return apiRequest(`/api/agents/${agentId}/metrics`)
}

export async function listFileRevisions(
  agentId: string,
  path: string,
): Promise<Array<{ id: string; operation?: string; created_at?: string | null }>> {
  return apiRequest(`/api/agents/${agentId}/files/revisions?path=${encodeURIComponent(path)}`)
}

export async function restoreFileRevision(agentId: string, revisionId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/files/restore`, { method: 'POST', body: { revision_id: revisionId } })
}

export type FileDownloadTicket = {
  readonly ticket: string
  readonly expires_in: number
}

export function mintFileDownloadTicket(
  agentId: string,
  path: string,
  { inline }: { readonly inline: boolean },
): Promise<FileDownloadTicket> {
  return apiRequest(`/api/agents/${encodeURIComponent(agentId)}/files/download-ticket`, {
    method: 'POST',
    body: { path, inline },
  })
}

export function fileTicketUrl(
  agentId: string,
  path: string,
  ticket: string,
  { inline }: { readonly inline: boolean },
): string {
  const query = new URLSearchParams({ path, inline: String(inline), ticket })
  return `/api/agents/${encodeURIComponent(agentId)}/files/download?${query.toString()}`
}

export async function listAgentToolsWithConfig(agentId: string): Promise<
  Array<
    AgentTool & {
      config_schema?: Record<string, unknown>
      agent_config?: Record<string, unknown>
      global_config?: Record<string, unknown>
    }
  >
> {
  return apiRequest(`/api/tools/agents/${agentId}/with-config`)
}

export async function testEmailConfig(config: Record<string, unknown>): Promise<{ ok?: boolean; error?: string }> {
  return apiRequest('/api/tools/test-email', { method: 'POST', body: { config } })
}

export async function testMcpServer(serverUrl: string, apiKey?: string): Promise<{ ok?: boolean; error?: string }> {
  return apiRequest('/api/tools/test-mcp', { method: 'POST', body: { server_url: serverUrl, api_key: apiKey } })
}

export async function importSkillFromUrl(url: string): Promise<unknown> {
  return apiRequest('/api/skills/import-from-url', { method: 'POST', body: { url } })
}
