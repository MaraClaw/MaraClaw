import { ApiError, apiRequest } from './http.ts'
import { ChannelConfigOutSchema } from './engine-contracts.generated.ts'
export type { ChannelConfigOut as ChannelConfig } from './engine-contracts.generated.ts'
import type { ChannelConfigOut as ChannelConfig } from './engine-contracts.generated.ts'


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
  { key: 'feishu', path: 'channel', webhook: true, label: 'Feishu' },
  { key: 'slack', path: 'slack-channel', webhook: true, label: 'Slack' },
  { key: 'discord', path: 'discord-channel', webhook: true, label: 'Discord' },
  { key: 'teams', path: 'teams-channel', webhook: true, label: 'Microsoft Teams' },
  { key: 'google-chat', path: 'google-chat-channel', webhook: true, label: 'Google Chat' },
  { key: 'wecom', path: 'wecom-channel', webhook: true, label: 'WeCom' },
  { key: 'dingtalk', path: 'dingtalk-channel', webhook: false, label: 'DingTalk' },
  { key: 'atlassian', path: 'atlassian-channel', webhook: false, label: 'Atlassian' },
  { key: 'whatsapp', path: 'whatsapp-channel', webhook: true, label: 'WhatsApp' },
] as const

export async function getChannel(agentId: string, path: string): Promise<ChannelConfig | null> {
  try {
    return await apiRequest(`/api/agents/${agentId}/${path}`, { schema: ChannelConfigOutSchema })
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null
    throw error
  }
}

export async function saveChannel(agentId: string, path: string, body: Record<string, string>): Promise<ChannelConfig> {
  return apiRequest(`/api/agents/${agentId}/${path}`, { schema: ChannelConfigOutSchema, method: 'POST', body })
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
