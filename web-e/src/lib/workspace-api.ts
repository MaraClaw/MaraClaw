import { apiFormRequest, apiRequest } from '@/lib/http'

export type AgentTemplate = {
  id: string
  name: string
  description?: string | null
  icon?: string | null
  category?: string | null
  capability_bullets?: string[]
}

export type AgentOut = {
  id: string
  name: string
  avatar_url?: string | null
  role_description: string
  bio?: string | null
  welcome_message?: string | null
  status: string
  creator_id: string
  is_expired?: boolean
  is_system?: boolean
  access_mode?: string
  agent_type?: string
  unread_count?: number
  onboarded_for_me?: boolean
  access_level?: 'use' | 'manage' | string
  created_at?: string
  heartbeat_enabled?: boolean
  heartbeat_interval_minutes?: number
  heartbeat_active_hours?: string
  timezone?: string | null
  gogcli_enabled?: boolean
  min_poll_interval_min?: number
  webhook_rate_limit?: number
  max_triggers?: number
}

export type ChatSession = {
  id: string
  agent_id: string
  user_id: string
  title: string
  source_channel: string
  created_at: string
  last_message_at?: string | null
  message_count: number
  unread_count: number
  is_primary?: boolean
}

export type ChatMessage = {
  role: string
  content: string
  created_at?: string | null
  file_name?: string
  toolName?: string
  toolStatus?: string
  toolResult?: string
  thinking?: string
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

export type TaskOut = {
  id: string
  title: string
  description?: string | null
  type?: string
  status?: string
  priority?: string
  created_at?: string
}

export type ScheduleOut = {
  id: string
  name: string
  instruction: string
  cron_expr: string
  is_enabled: boolean
  next_run_at?: string | null
  last_run_at?: string | null
}

export type FocusItem = {
  key: string
  title: string
  description?: string
  status: string
  kind?: string
}

export type NotificationRow = {
  id: string
  title: string
  body?: string
  link?: string | null
  is_read?: boolean
  created_at?: string
}

export type OnboardingStatus = {
  exists: boolean
  status: string
  current_step: string
  entry_mode?: string | null
  personal_assistant_agent_id?: string | null
}

export type ChannelConfig = {
  id?: string
  agent_id?: string
  channel_type?: string
  webhook_url?: string | null
  extra_config?: Record<string, unknown>
}

export async function listAgents(): Promise<AgentOut[]> {
  return apiRequest('/api/agents/')
}

export async function listTemplates(): Promise<AgentTemplate[]> {
  return apiRequest('/api/agents/templates')
}

export async function getAgent(agentId: string): Promise<AgentOut> {
  return apiRequest(`/api/agents/${agentId}`)
}

export async function createAgent(body: {
  name: string
  template_id?: string
  role_description?: string
  permission_scope_type?: string
  permission_access_level?: string
  agent_type?: string
  gogcli_enabled?: boolean
}): Promise<AgentOut> {
  return apiRequest('/api/agents/', { method: 'POST', body })
}

export async function updateAgent(agentId: string, body: Record<string, unknown>): Promise<AgentOut> {
  return apiRequest(`/api/agents/${agentId}`, { method: 'PATCH', body })
}

export async function deleteAgent(agentId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}`, { method: 'DELETE' })
}

export async function startAgent(agentId: string): Promise<AgentOut> {
  return apiRequest(`/api/agents/${agentId}/start`, { method: 'POST' })
}

export async function stopAgent(agentId: string): Promise<AgentOut> {
  return apiRequest(`/api/agents/${agentId}/stop`, { method: 'POST' })
}

export async function getOnboardingStatus(): Promise<OnboardingStatus> {
  return apiRequest('/api/onboarding/status')
}

export async function startOnboarding(entryMode = 'join'): Promise<OnboardingStatus> {
  return apiRequest('/api/onboarding/start', { method: 'POST', body: { entry_mode: entryMode } })
}

export async function createPersonalAssistant(body: {
  name: string
  personality?: string
  work_style?: string
  boundaries?: string
}): Promise<{ agent: { id: string; name: string }; onboarding: OnboardingStatus }> {
  return apiRequest('/api/onboarding/personal-assistant', { method: 'POST', body })
}

export async function completeOnboarding(): Promise<OnboardingStatus> {
  return apiRequest('/api/onboarding/complete', { method: 'POST' })
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
  extracted_text?: string
  path?: string
}> {
  const form = new FormData()
  form.append('file', file)
  form.append('agent_id', agentId)
  return apiFormRequest('/api/upload', form)
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

export async function listTasks(agentId: string): Promise<TaskOut[]> {
  return apiRequest(`/api/agents/${agentId}/tasks/`)
}

export async function createTask(agentId: string, body: { title: string; description?: string; type?: string }): Promise<TaskOut> {
  return apiRequest(`/api/agents/${agentId}/tasks/`, { method: 'POST', body })
}

export async function triggerTask(agentId: string, taskId: string): Promise<unknown> {
  return apiRequest(`/api/agents/${agentId}/tasks/${taskId}/trigger`, { method: 'POST' })
}

export async function listSchedules(agentId: string): Promise<ScheduleOut[]> {
  return apiRequest(`/api/agents/${agentId}/schedules/`)
}

export async function createSchedule(
  agentId: string,
  body: { name: string; instruction: string; cron_expr: string; is_enabled?: boolean },
): Promise<ScheduleOut> {
  return apiRequest(`/api/agents/${agentId}/schedules/`, { method: 'POST', body })
}

export async function runSchedule(agentId: string, scheduleId: string): Promise<unknown> {
  return apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}/run`, { method: 'POST' })
}

export async function deleteSchedule(agentId: string, scheduleId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}`, { method: 'DELETE' })
}

export async function listFocus(agentId: string): Promise<FocusItem[]> {
  return apiRequest(`/api/agents/${agentId}/focus/`)
}

export async function upsertFocus(agentId: string, body: {
  key: string
  title: string
  description?: string
  status?: string
  kind?: string
}): Promise<FocusItem> {
  return apiRequest(`/api/agents/${agentId}/focus/`, { method: 'POST', body })
}

export async function completeFocus(agentId: string, key: string): Promise<FocusItem> {
  return apiRequest(`/api/agents/${agentId}/focus/${encodeURIComponent(key)}/complete`, { method: 'POST' })
}

export async function listTriggers(agentId: string): Promise<Array<{
  id: string
  name: string
  type: string
  is_enabled: boolean
}>> {
  return apiRequest(`/api/agents/${agentId}/triggers`)
}

export async function patchTrigger(agentId: string, triggerId: string, body: { is_enabled?: boolean }): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/triggers/${triggerId}`, { method: 'PATCH', body })
}

export async function listNotifications(): Promise<NotificationRow[]> {
  return apiRequest('/api/notifications')
}

export async function unreadNotificationCount(): Promise<{ count?: number; unread_count?: number }> {
  return apiRequest('/api/notifications/unread-count')
}

export async function markNotificationRead(id: string): Promise<void> {
  await apiRequest(`/api/notifications/${id}/read`, { method: 'POST' })
}

export async function markAllNotificationsRead(): Promise<void> {
  await apiRequest('/api/notifications/read-all', { method: 'POST' })
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

export async function listTaskLogs(agentId: string, taskId: string): Promise<Array<{ id?: string; content: string; created_at?: string }>> {
  return apiRequest(`/api/agents/${agentId}/tasks/${taskId}/logs`)
}

export async function updateSchedule(
  agentId: string,
  scheduleId: string,
  body: { name?: string; instruction?: string; cron_expr?: string; is_enabled?: boolean },
): Promise<ScheduleOut> {
  return apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}`, { method: 'PATCH', body })
}

export async function listScheduleHistory(
  agentId: string,
  scheduleId: string,
): Promise<Array<{ id: string; created_at?: string | null; summary?: string; reply?: string }>> {
  return apiRequest(`/api/agents/${agentId}/schedules/${scheduleId}/history`)
}

export async function deleteTrigger(agentId: string, triggerId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/triggers/${triggerId}`, { method: 'DELETE' })
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

export type ChannelField = { key: string; label: string; secret?: boolean }

export const CHANNEL_FIELDS: Record<string, ChannelField[]> = {
  feishu: [
    { key: 'app_id', label: 'App ID' },
    { key: 'app_secret', label: 'App secret', secret: true },
    { key: 'encrypt_key', label: 'Encrypt key', secret: true },
    { key: 'verification_token', label: 'Verification token', secret: true },
  ],
  slack: [
    { key: 'bot_token', label: 'Bot token', secret: true },
    { key: 'signing_secret', label: 'Signing secret', secret: true },
  ],
  discord: [
    { key: 'bot_token', label: 'Bot token', secret: true },
    { key: 'application_id', label: 'Application ID' },
    { key: 'public_key', label: 'Public key' },
    { key: 'connection_mode', label: 'Mode (webhook or gateway)' },
  ],
  teams: [
    { key: 'app_id', label: 'App ID' },
    { key: 'app_secret', label: 'App secret', secret: true },
    { key: 'tenant_id', label: 'Azure tenant ID' },
  ],
  'google-chat': [
    { key: 'project_number', label: 'Project number' },
    { key: 'client_email', label: 'Client email' },
    { key: 'audience', label: 'Audience' },
    { key: 'verification_token', label: 'Verification token', secret: true },
  ],
  wecom: [
    { key: 'bot_id', label: 'Bot ID' },
    { key: 'bot_secret', label: 'Bot secret', secret: true },
    { key: 'corp_id', label: 'Corp ID' },
    { key: 'secret', label: 'Secret', secret: true },
    { key: 'token', label: 'Token', secret: true },
    { key: 'encoding_aes_key', label: 'Encoding AES key', secret: true },
  ],
  dingtalk: [
    { key: 'app_key', label: 'App key' },
    { key: 'app_secret', label: 'App secret', secret: true },
  ],
  atlassian: [
    { key: 'api_key', label: 'API key', secret: true },
    { key: 'cloud_id', label: 'Cloud ID' },
  ],
  whatsapp: [
    { key: 'access_token', label: 'Access token', secret: true },
    { key: 'phone_number_id', label: 'Phone number ID' },
    { key: 'verify_token', label: 'Verify token', secret: true },
    { key: 'app_secret', label: 'App secret', secret: true },
    { key: 'api_version', label: 'API version' },
  ],
}

export const CHANNELS = [
  { key: 'feishu', path: 'channel', label: 'Feishu' },
  { key: 'slack', path: 'slack-channel', label: 'Slack' },
  { key: 'discord', path: 'discord-channel', label: 'Discord' },
  { key: 'teams', path: 'teams-channel', label: 'Microsoft Teams' },
  { key: 'google-chat', path: 'google-chat-channel', label: 'Google Chat' },
  { key: 'wecom', path: 'wecom-channel', label: 'WeCom' },
  { key: 'dingtalk', path: 'dingtalk-channel', label: 'DingTalk' },
  { key: 'atlassian', path: 'atlassian-channel', label: 'Atlassian' },
  { key: 'whatsapp', path: 'whatsapp-channel', label: 'WhatsApp' },
] as const

export async function getChannel(agentId: string, path: string): Promise<ChannelConfig | null> {
  try {
    return await apiRequest(`/api/agents/${agentId}/${path}`)
  } catch {
    return null
  }
}

export async function saveChannel(agentId: string, path: string, body: Record<string, string>): Promise<ChannelConfig> {
  return apiRequest(`/api/agents/${agentId}/${path}`, { method: 'POST', body })
}

export async function deleteChannel(agentId: string, path: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/${path}`, { method: 'DELETE' })
}

export async function getChannelWebhook(agentId: string, path: string): Promise<{ url?: string; webhook_url?: string }> {
  return apiRequest(`/api/agents/${agentId}/${path}/webhook-url`)
}

export async function getWechatQr(agentId: string): Promise<{ qrcode_url?: string; ticket?: string }> {
  return apiRequest(`/api/agents/${agentId}/wechat-channel/qrcode`, { method: 'POST' })
}

export type AgentPermissions = {
  can_manage?: boolean
  is_owner?: boolean
  access_level?: string
  effective_access_level?: string
  scope_type?: string
  user_access?: Array<{ id: string; name?: string; email?: string; access_level?: string }>
}

export async function getAgentPermissions(agentId: string): Promise<AgentPermissions> {
  return apiRequest(`/api/agents/${agentId}/permissions`)
}

export async function updateAgentPermissions(
  agentId: string,
  body: {
    scope_type: string
    access_level?: string
    scope_ids?: string[]
    user_access?: Array<{ id: string; access_level: string }>
  },
): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/permissions`, { method: 'PUT', body })
}

export async function listPermissionCandidates(
  agentId: string,
): Promise<Array<{ id: string; name?: string; email?: string; username?: string }>> {
  const data = await apiRequest<{ users?: Array<{ id: string; name?: string; email?: string; username?: string }> }>(
    `/api/agents/${agentId}/permissions/candidates`,
  )
  return data.users ?? []
}

export async function handoverAgent(agentId: string, newCreatorId: string): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/handover`, { method: 'POST', body: { new_creator_id: newCreatorId } })
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

export function fileDownloadUrl(agentId: string, path: string, token: string): string {
  const query = new URLSearchParams({ path, token })
  return `/api/agents/${agentId}/files/download?${query.toString()}`
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

export const ONBOARDING_SKIP_KEY = 'maraclaw-onboarding-skipped'

export function markOnboardingSkipped(): void {
  try {
    localStorage.setItem(ONBOARDING_SKIP_KEY, '1')
  } catch {
    // ignore
  }
}

export function wasOnboardingSkipped(): boolean {
  try {
    return localStorage.getItem(ONBOARDING_SKIP_KEY) === '1'
  } catch {
    return false
  }
}
