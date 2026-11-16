import { z } from 'zod'

import { apiRequest, type RequestOptions } from './http.ts'

export type LinkChannel = 'telegram' | 'zalo'
type LinkTarget = { readonly agentId: string; readonly channel: LinkChannel }
type LinkContext = Required<Pick<RequestOptions, 'owner' | 'signal'>>
const channelSchema = z.enum(['telegram', 'zalo'])
const linkStatusSchema = z.object({
  channel: channelSchema,
  available: z.boolean(),
  linked: z.boolean(),
  linked_at: z.iso.datetime().nullable(),
  external_hint: z.string().nullable(),
}).readonly()
const linkCodeSchema = z.object({
  channel: channelSchema,
  code: z.string().regex(/^[A-Z0-9]{10}$/),
  expires_at: z.iso.datetime(),
  deep_link: z.url({ protocol: /^(https|tg|zalo)$/ }).nullable(),
  account_hint: z.string().nullable(),
}).readonly()
export type ChannelLinkStatus = z.infer<typeof linkStatusSchema>
export type ChannelLinkCode = z.infer<typeof linkCodeSchema>

export function getChannelLink(target: LinkTarget, context: LinkContext): Promise<ChannelLinkStatus> {
  return apiRequest(`/api/agents/${target.agentId}/channel-links/${target.channel === 'telegram' ? 'telegram' : 'zalo'}`, {
    owner: context.owner, signal: context.signal, cache: 'no-store', schema: linkStatusSchema.refine((value) => value.channel === target.channel),
  })
}

export function issueChannelLinkCode(target: LinkTarget, context: LinkContext): Promise<ChannelLinkCode> {
  return apiRequest(`/api/agents/${target.agentId}/channel-links/${target.channel === 'telegram' ? 'telegram' : 'zalo'}/code`, {
    owner: context.owner, signal: context.signal, method: 'POST', cache: 'no-store', schema: linkCodeSchema.refine((value) => value.channel === target.channel),
  })
}

export async function unlinkChannelAccount(target: LinkTarget, context: LinkContext): Promise<void> {
  await apiRequest(`/api/agents/${target.agentId}/channel-links/${target.channel === 'telegram' ? 'telegram' : 'zalo'}`, {
    owner: context.owner, signal: context.signal, method: 'DELETE', cache: 'no-store',
  })
}
