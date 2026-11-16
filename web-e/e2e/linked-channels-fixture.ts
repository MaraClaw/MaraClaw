import { expect, type Page } from '@playwright/test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QRCodeSVG } from 'qrcode.react'

import { channelApiRoot, prepareChannels } from './channels-fixture'
import { originalAgent, originalToken, originalUser, replacementUser, replacementToken } from './session-fencing-fixture'
import type { ChannelCapability } from '../src/lib/channels-api'
import type { ZaloPersonalStatus } from '../src/lib/zalo-api'

export const linkedCatalog: readonly ChannelCapability[] = [
  { channel_type: 'telegram', display_name: 'Telegram bot', transport: 'webhook', supports_inbound: true, supports_proactive: false },
  { channel_type: 'zalo', display_name: 'Zalo personal', transport: 'poll', supports_inbound: true, supports_proactive: false },
]
export const generation = '11111111-1111-4111-8111-111111111111'
export const nextGeneration = '22222222-2222-4222-8222-222222222222'
export const disconnected: ZaloPersonalStatus = { mode: 'personal_qr', generation: null, attempt_id: null, revision: 0, state: 'disconnected', expires_at: null, qr: null, account_hint: null, error_code: null, resumable: false, diagnostics: null }

export async function prepareLinkedChannels(page: Page, options: { readonly creator?: boolean; readonly qr?: boolean; readonly status?: ZaloPersonalStatus } = {}) {
  await prepareChannels(page, { catalog: linkedCatalog, creator: options.creator ?? true })
  const requests: string[] = []
  const telegramWrites: unknown[] = []
  const linked = { telegram: false, zalo: false }
  const syntheticPng = await page.evaluate(async (svg) => {
    const image = new Image()
    image.src = `data:image/svg+xml,${encodeURIComponent(svg)}`
    await image.decode()
    const canvas = document.createElement('canvas')
    canvas.width = 240; canvas.height = 240
    const context = canvas.getContext('2d')
    if (!context) throw new Error('Canvas unavailable for synthetic QR fixture')
    context.drawImage(image, 0, 0)
    return canvas.toDataURL('image/png').split(',')[1] ?? ''
  }, renderToStaticMarkup(createElement(QRCodeSVG, { xmlns: 'http://www.w3.org/2000/svg', value: 'synthetic-zalo-pairing-only', size: 240, marginSize: 4 })))
  let configured = false
  let status: ZaloPersonalStatus = options.status ?? disconnected
  const pairing = async (value: string): Promise<ZaloPersonalStatus> => {
    const now = await page.evaluate(() => Date.now())
    return {
    ...disconnected, generation: value, attempt_id: value, revision: status.revision + 1, state: 'qr',
    expires_at: new Date(now + 60000).toISOString(),
    qr: { value: syntheticPng, expires_at: new Date(now + 10000).toISOString() },
    }
  }
  if (options.qr) status = await pairing(generation)
  const telegramConfig = { id: generation, agent_id: originalAgent.id, channel_type: 'telegram', app_id: 'synthetic_bot', app_secret: '***', extra_config: { secret_token: '***' }, is_configured: true, is_connected: false }
  await page.route(`**${channelApiRoot}`, (route) => route.fulfill({ json: {
    ...originalAgent, creator_id: options.creator === false ? replacementUser.id : originalUser.id,
    access_level: options.creator === false ? 'use' : 'manage',
  } }))
  await page.route(`**${channelApiRoot}/*channel**`, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.slice(channelApiRoot.length)
    const method = request.method()
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(request.headers().authorization)
    requests.push(`${method} ${path}`)
    if (path.startsWith('/channel-links/')) {
      const channel = path.split('/')[2]
      expect(channel === 'telegram' || channel === 'zalo').toBe(true)
      if (channel !== 'telegram' && channel !== 'zalo') throw new Error('Unexpected link channel')
      if (path.endsWith('/code')) {
        expect(method).toBe('POST')
        return route.fulfill({ status: 201, json: {
          channel, code: channel === 'telegram' ? 'ABCDE12345' : 'FGHJK67890', expires_at: new Date(Date.now() + 600000).toISOString(),
          deep_link: channel === 'telegram' ? 'https://t.me/synthetic_bot?start=ABCDE12345' : null, account_hint: channel === 'telegram' ? '@synthetic_bot' : 'Synthetic Zalo agent',
        } })
      }
      if (method === 'DELETE') { linked[channel] = false; return route.fulfill({ status: 204 }) }
      expect(method).toBe('GET')
      return route.fulfill({ json: { channel, available: true, linked: linked[channel], linked_at: linked[channel] ? '2030-01-01T00:00:00Z' : null, external_hint: linked[channel] ? '••42' : null } })
    }
    if (path === '/telegram-channel/webhook-url') {
      expect(method).toBe('GET')
      return route.fulfill({ json: { webhook_url: 'https://synthetic.test/api/telegram/webhook/agent' } })
    }
    if (path === '/telegram-channel') {
      switch (method) {
        case 'GET': return route.fulfill(configured ? { json: telegramConfig } : { status: 404, json: { detail: 'Not configured' } })
        case 'POST': telegramWrites.push(request.postDataJSON()); configured = true; return route.fulfill({ status: 201, json: telegramConfig })
        case 'DELETE': configured = false; return route.fulfill({ status: 204 })
        default: throw new Error(`Unexpected Telegram method ${method}`)
      }
    }
    if (path === '/zalo-channel/personal/start') {
      expect(method).toBe('POST'); expect(request.postDataJSON()).toEqual({ expected_generation: status.generation })
      status = status.resumable && status.generation !== null && (status.state === 'paused' || status.state === 'failed')
        ? { ...status, revision: status.revision + 1, state: 'connected', qr: null, error_code: null, diagnostics: status.diagnostics ? { ...status.diagnostics, retry_at: null, heartbeat_failures: 0, last_error: null, last_error_at: null } : null }
        : await pairing(generation)
    } else if (path === '/zalo-channel/personal/relink') {
      expect(method).toBe('POST'); expect(request.postDataJSON()).toEqual({ expected_generation: status.generation })
      status = await pairing(status.generation === generation ? nextGeneration : generation)
    } else if (path === '/zalo-channel/personal' && method === 'DELETE') {
      expect(url.searchParams.get('expected_generation')).toBe(status.generation)
      status = { ...status, revision: status.revision + 1, state: 'disconnected', qr: null }
    } else {
      expect(path).toBe('/zalo-channel/personal'); expect(method).toBe('GET')
    }
    return route.fulfill({ json: status })
  })
  return { requests, telegramWrites, linked, connectZalo() { status = { ...status, revision: status.revision + 1, state: 'connected', qr: null, account_hint: '••24' } } }
}
