import { expect, type Page } from '@playwright/test'
import { z } from 'zod'

import type { ChannelCapability } from '../src/lib/channels-api'
import { originalAgent, originalToken, prepareWorkspace, replacementToken, replacementUser } from './session-fencing-fixture'

export const channelsPath = `/app/agents/${originalAgent.id}/channels`
export const channelApiRoot = `/api/agents/${originalAgent.id}`
export const channelCatalog: readonly ChannelCapability[] = [
  { channel_type: 'feishu', display_name: 'Feishu', transport: 'stream', supports_inbound: true, supports_proactive: true },
  { channel_type: 'wecom', display_name: 'WeCom', transport: 'stream', supports_inbound: true, supports_proactive: true },
  { channel_type: 'wechat', display_name: 'WeChat', transport: 'poll', supports_inbound: true, supports_proactive: true },
  { channel_type: 'whatsapp', display_name: 'WhatsApp', transport: 'webhook', supports_inbound: true, supports_proactive: false },
  { channel_type: 'dingtalk', display_name: 'DingTalk', transport: 'stream', supports_inbound: true, supports_proactive: true },
  { channel_type: 'slack', display_name: 'Slack', transport: 'webhook', supports_inbound: true, supports_proactive: true },
  { channel_type: 'discord', display_name: 'Discord', transport: 'gateway', supports_inbound: true, supports_proactive: false },
  { channel_type: 'microsoft_teams', display_name: 'MS Teams', transport: 'webhook', supports_inbound: true, supports_proactive: true },
  { channel_type: 'google_chat', display_name: 'Google Chat', transport: 'webhook', supports_inbound: true, supports_proactive: true },
  { channel_type: 'atlassian', display_name: 'Atlassian', transport: 'skill_only', supports_inbound: false, supports_proactive: false },
  { channel_type: 'agentbay', display_name: 'AgentBay', transport: 'skill_only', supports_inbound: false, supports_proactive: false },
]
export const googleConfig = {
  id: '11111111-1111-4111-8111-111111111111', agent_id: originalAgent.id, channel_type: 'google_chat',
  app_id: '123456', app_secret: '***', is_configured: true, is_connected: false,
  extra_config: {
    audience: 'stored-audience',
    service_account_json: { type: 'service_account', project_id: 'synthetic-project', client_email: 'synthetic@provider.test', credentials_configured: true },
  },
}

export async function prepareChannels(page: Page, options: { readonly creator?: boolean; readonly configured?: boolean; readonly serviceAccount?: boolean; readonly catalog?: readonly ChannelCapability[] } = {}) {
  await prepareWorkspace(page)
  const config = options.serviceAccount === false
    ? { ...googleConfig, app_secret: null, extra_config: { audience: 'stored-audience' } }
    : googleConfig
  const reads: string[] = []
  const writes: Record<string, string>[] = []
  await page.route(`**${channelApiRoot}`, (route) => {
    const replacing = route.request().headers().authorization === `Bearer ${replacementToken}`
    return route.fulfill({ json: { ...originalAgent, creator_id: options.creator === false || replacing ? replacementUser.id : originalAgent.creator_id } })
  })
  await page.route(`**${channelApiRoot}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route('**/api/channel-catalog', (route) => route.fulfill({ json: options.catalog ?? channelCatalog }))
  await page.route(`**${channelApiRoot}/*channel**`, async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(request.headers().authorization)
    reads.push(`${request.method()} ${path}`)
    if (path.endsWith('/qrcode')) {
      expect(request.method()).toBe('POST')
      await route.fulfill({ json: { qrcode_url: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192"><rect width="192" height="192" fill="black"/></svg>' } })
      return
    }
    if (path.endsWith('/webhook-url')) {
      expect(request.method()).toBe('GET')
      await route.fulfill({ json: { webhook_url: 'https://synthetic.test/google-chat/webhook' } })
      return
    }
    const google = path.endsWith('/google-chat-channel')
    switch (request.method()) {
      case 'GET':
        await route.fulfill(google && options.configured !== false ? { json: config } : { status: 404, json: { detail: 'Not configured' } })
        return
      case 'POST': {
        expect(google).toBe(true)
        const body = z.record(z.string(), z.string()).parse(request.postDataJSON())
        writes.push(body)
        await route.fulfill({ status: 201, json: { ...config, app_id: body.project_number ?? config.app_id } })
        return
      }
      default:
        throw new Error(`Unexpected channel fixture method ${request.method()}`)
    }
  })
  return { reads, writes }
}
