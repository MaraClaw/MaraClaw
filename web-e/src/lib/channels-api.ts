import { z } from 'zod'

import { ApiError, apiRequest, type RequestOptions } from './http.ts'
import { ChannelConfigOutSchema } from './engine-contracts.generated.ts'
export type { ChannelConfigOut as ChannelConfig } from './engine-contracts.generated.ts'
import type { ChannelConfigOut as ChannelConfig } from './engine-contracts.generated.ts'


export type ChannelField = { readonly key: string; readonly label: string; readonly secret?: boolean; readonly multiline?: boolean }

const channelCapabilitySchema = z.object({
  channel_type: z.string().min(1),
  display_name: z.string().min(1),
  transport: z.enum(['webhook', 'stream', 'gateway', 'poll', 'skill_only']),
  supports_inbound: z.boolean(),
  supports_proactive: z.boolean(),
}).readonly()
const channelCatalogSchema = z.array(channelCapabilitySchema).readonly()
export type ChannelCapability = z.infer<typeof channelCapabilitySchema>
type RequestContext = Pick<RequestOptions, 'owner' | 'signal'>

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
  microsoft_teams: [
    { key: 'app_id', label: 'App ID' },
    { key: 'app_secret', label: 'App secret', secret: true },
    { key: 'tenant_id', label: 'Azure tenant ID' },
  ],
  google_chat: [
    { key: 'project_number', label: 'Project number' },
    { key: 'client_email', label: 'Client email' },
    { key: 'audience', label: 'Audience' },
    { key: 'verification_token', label: 'Verification token', secret: true },
    { key: 'service_account_json', label: 'Service account JSON', secret: true, multiline: true },
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
  whatsapp: [
    { key: 'access_token', label: 'Access token', secret: true },
    { key: 'phone_number_id', label: 'Phone number ID' },
    { key: 'verify_token', label: 'Verify token', secret: true },
    { key: 'app_secret', label: 'App secret', secret: true },
    { key: 'api_version', label: 'API version' },
  ],
}

// REST endpoint metadata only (webhook means the webhook-url route exists). UI capabilities and names belong to the engine catalog.
export const CHANNELS = [
  { key: 'feishu', path: 'channel', webhook: true },
  { key: 'slack', path: 'slack-channel', webhook: true },
  { key: 'discord', path: 'discord-channel', webhook: true },
  { key: 'microsoft_teams', path: 'teams-channel', webhook: true },
  { key: 'google_chat', path: 'google-chat-channel', webhook: true },
  { key: 'wecom', path: 'wecom-channel', webhook: true },
  { key: 'dingtalk', path: 'dingtalk-channel', webhook: false },
  { key: 'whatsapp', path: 'whatsapp-channel', webhook: true },
] as const

export function googleChatCredentialsConfigured(config?: ChannelConfig | null): boolean {
  const account = config?.extra_config?.['service_account_json']
  return typeof account === 'object' && account !== null
    && 'credentials_configured' in account && account['credentials_configured'] === true
}

export function channelDraftBody(channelType: string, draft: Readonly<Record<string, string>>, current?: ChannelConfig | null): Record<string, string> {
  const body = { ...draft }
  for (const field of CHANNEL_FIELDS[channelType] ?? []) {
    if (field.secret && body[field.key] !== undefined) {
      body[field.key] = z.string().trim().min(1, 'Enter a fresh credential or leave the field untouched.')
        .refine((value) => !/\*{3,}|•{3,}|redacted|masked/i.test(value), 'Enter a fresh credential, not a masked value.')
        .parse(body[field.key])
    }
  }
  if (channelType === 'feishu') body['channel_type'] = 'feishu'
  if (channelType === 'google_chat' && body['project_number'] === undefined && current?.app_id) body['project_number'] = current.app_id
  return body
}

export async function getChannelCatalog(context: RequestContext = {}): Promise<readonly ChannelCapability[]> {
  return apiRequest('/api/channel-catalog', {
    schema: channelCatalogSchema,
    ...(context.owner === undefined ? {} : { owner: context.owner }),
    ...(context.signal === undefined ? {} : { signal: context.signal }),
  })
}

export async function getChannel(agentId: string, path: string, context: RequestContext = {}): Promise<ChannelConfig | null> {
  try {
    return await apiRequest(`/api/agents/${agentId}/${path}`, {
      schema: ChannelConfigOutSchema,
      ...(context.owner === undefined ? {} : { owner: context.owner }),
      ...(context.signal === undefined ? {} : { signal: context.signal }),
    })
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null
    throw error
  }
}

export async function saveChannel(target: { readonly agentId: string; readonly path: string }, body: Record<string, string>, context: RequestContext = {}): Promise<ChannelConfig> {
  const { agentId, path } = target
  return apiRequest(`/api/agents/${agentId}/${path}`, {
    schema: ChannelConfigOutSchema, method: 'POST', body,
    ...(context.owner === undefined ? {} : { owner: context.owner }),
    ...(context.signal === undefined ? {} : { signal: context.signal }),
  })
}

export async function deleteChannel(agentId: string, path: string, context: RequestContext = {}): Promise<void> {
  await apiRequest(`/api/agents/${agentId}/${path}`, {
    method: 'DELETE',
    ...(context.owner === undefined ? {} : { owner: context.owner }),
    ...(context.signal === undefined ? {} : { signal: context.signal }),
  })
}

export async function getChannelWebhook(agentId: string, path: string, context: RequestContext = {}): Promise<{ url?: string; webhook_url?: string }> {
  return apiRequest(`/api/agents/${agentId}/${path}/webhook-url`, {
    ...(context.owner === undefined ? {} : { owner: context.owner }),
    ...(context.signal === undefined ? {} : { signal: context.signal }),
  })
}

export async function getWechatQr(agentId: string, context: RequestContext = {}): Promise<{ qrcode_url?: string; ticket?: string }> {
  return apiRequest(`/api/agents/${agentId}/wechat-channel/qrcode`, {
    method: 'POST',
    ...(context.owner === undefined ? {} : { owner: context.owner }),
    ...(context.signal === undefined ? {} : { signal: context.signal }),
  })
}
