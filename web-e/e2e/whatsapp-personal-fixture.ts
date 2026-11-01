import { expect, type Page } from '@playwright/test'
import { randomUUID } from 'node:crypto'

import type { PersonalStatus } from '../src/lib/whatsapp-api'
import { channelApiRoot, prepareChannels } from './channels-fixture'
import { originalAgent, originalToken, replacementToken } from './session-fencing-fixture'

export const personalRoot = `${channelApiRoot}/whatsapp-channel/personal`
export const personalGeneration = '11111111-1111-4111-8111-111111111111'
export const personalAttempt = '22222222-2222-4222-8222-222222222222'
export const unlinked: PersonalStatus = { mode: 'personal_qr', generation: null, attempt_id: null, revision: 0, state: 'disconnected', expires_at: null, qr: null, account_hint: null, error_code: null }
export const syntheticQr: PersonalStatus = { ...unlinked, generation: personalGeneration, attempt_id: personalAttempt, revision: 1, state: 'qr', expires_at: '2030-01-01T00:02:00Z', qr: { value: 'synthetic-whatsapp-pairing-only-not-a-real-account', expires_at: '2030-01-01T00:01:00Z' } }

export async function preparePersonal(page: Page, options: { readonly creator?: boolean; readonly status?: PersonalStatus; readonly mode?: 'cloud' | 'personal_qr'; readonly liveChild?: boolean } = {}) {
  const channels = await prepareChannels(page, { ...(options.creator === undefined ? {} : { creator: options.creator }) })
  let status = options.status ?? unlinked
  let mode = options.mode ?? 'cloud'
  let liveChild = options.liveChild ?? status.attempt_id !== null
  let relinking = false
  const operations: string[] = []
  const bodies: unknown[] = []
  await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
  await page.route(`**${channelApiRoot}/whatsapp-channel`, async (route) => {
    const request = route.request()
    operations.push(`${request.method()} config`)
    if (request.method() === 'GET') return route.fulfill({ json: { id: personalGeneration, agent_id: originalAgent.id, channel_type: 'whatsapp', app_id: null, app_secret: null, is_configured: true, is_connected: false, extra_config: null, mode } })
    expect(request.method()).toBe('POST')
    bodies.push(request.postDataJSON())
    mode = 'cloud'
    return route.fulfill({ status: 201, json: { id: personalGeneration, agent_id: originalAgent.id, channel_type: 'whatsapp', app_id: null, app_secret: null, is_configured: true, is_connected: false, extra_config: null, mode } })
  })
  await page.route(`**${personalRoot}**`, async (route) => {
    const request = route.request()
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(request.headers().authorization)
    const url = new URL(request.url())
    operations.push(`${request.method()} ${url.pathname.slice(personalRoot.length) || 'status'}`)
    if (request.method() === 'GET') return route.fulfill({ headers: { 'Cache-Control': 'no-store, private' }, json: status })
    if (request.method() === 'DELETE') {
      expect(url.searchParams.get('expected_generation')).toBe(status.generation)
      relinking = false
      status = { ...status, generation: randomUUID(), revision: status.revision + 1, state: liveChild ? 'disconnecting' : 'disconnected', qr: null, account_hint: liveChild ? status.account_hint : null }
      return route.fulfill({ json: status })
    }
    expect(request.method()).toBe('POST')
    expect(request.postDataJSON()).toEqual({ expected_generation: status.generation })
    bodies.push(request.postDataJSON())
    relinking = url.pathname.endsWith('/relink') && liveChild
    status = relinking
      ? { ...status, generation: randomUUID(), revision: status.revision + 1, state: 'disconnecting', qr: null }
      : { ...unlinked, generation: status.generation === null ? personalGeneration : randomUUID(), attempt_id: status.attempt_id === null ? personalAttempt : randomUUID(), revision: status.revision + 1, state: 'pending', expires_at: new Date(await page.evaluate(() => Date.now()) + 120000).toISOString() }
    mode = 'personal_qr'
    return route.fulfill({ status: 202, headers: { 'Cache-Control': 'no-store, private' }, json: status })
  })
  return {
    ...channels, operations, bodies, getStatus: () => status,
    setStatus: (value: PersonalStatus) => { status = value },
    async reap() {
      liveChild = false
      status = { ...status, attempt_id: relinking ? randomUUID() : status.attempt_id, revision: status.revision + 1, state: relinking ? 'pending' : 'disconnected', qr: null, account_hint: null, expires_at: relinking ? new Date(await page.evaluate(() => Date.now()) + 120000).toISOString() : status.expires_at }
    },
  }
}
