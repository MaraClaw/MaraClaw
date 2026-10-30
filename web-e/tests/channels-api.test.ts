import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { CHANNELS, channelDraftBody, getChannelCatalog, getChannel, googleChatCredentialsConfigured, saveChannel, type ChannelConfig } from '../src/lib/channels-api.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'

const id = '11111111-1111-4111-8111-111111111111'
const stored: ChannelConfig = {
  id, agent_id: id, channel_type: 'google_chat', app_id: '123456', app_secret: '***',
  is_configured: true, is_connected: false,
  extra_config: {
    audience: 'stored-audience',
    service_account_json: { type: 'service_account', project_id: 'synthetic-project', client_email: 'synthetic@provider.test', credentials_configured: true },
  },
}
const capability = { channel_type: 'google_chat', display_name: 'Engine Chat', transport: 'webhook', supports_inbound: true, supports_proactive: false } as const

test('Google Chat reports configured when engine returns nested redacted service-account metadata', () => {
  assert.equal(googleChatCredentialsConfigured(stored), true)
})

for (const extra_config of [
  null, {}, { audience: 'stored-audience' }, { credentials_configured: true },
  { service_account_json: null }, { service_account_json: '***' }, { service_account_json: true },
  { service_account_json: [] }, { service_account_json: { client_email: 'synthetic@provider.test' } },
  { service_account_json: { credentials_configured: false } }, { service_account_json: { credentials_configured: 'true' } },
]) {
  test('Google Chat remains inbound-only when the nested configured flag is absent or invalid', () => {
    assert.equal(googleChatCredentialsConfigured({ ...stored, extra_config }), false)
  })
}

test('Google Chat remains inbound-only when no configuration exists', () => {
  assert.equal(googleChatCredentialsConfigured(null), false)
  assert.equal(googleChatCredentialsConfigured(undefined), false)
})

test('catalog parses engine names and flags and carries request cancellation', async (t) => {
  const signal = new AbortController().signal
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    assert.equal(String(input), '/api/channel-catalog')
    assert.equal(init?.signal, signal)
    return Response.json([capability, { ...capability, channel_type: 'agentbay', transport: 'skill_only' }])
  })
  const catalog = await getChannelCatalog({ owner: authSession.capture(), signal })
  assert.equal(catalog[0]?.display_name, 'Engine Chat')
  assert.equal(catalog[0]?.supports_proactive, false)
  assert.equal(catalog[1]?.channel_type, 'agentbay')
})

for (const response of [[{ ...capability, supports_inbound: 'true' }], { channels: [capability] }, [{ ...capability, transport: 'unknown' }]]) {
  test('catalog rejects malformed responses instead of installing fallback capabilities', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json(response))
    await assert.rejects(getChannelCatalog(), ZodError)
  })
}

test('catalog failure remains retryable rather than a successful fallback', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  await assert.rejects(getChannelCatalog(), ApiError)
})

test('Google Chat unrelated edits omit stored email credentials and audience', () => {
  const body = channelDraftBody('google_chat', { project_number: '654321' }, stored)
  assert.deepEqual(body, { project_number: '654321' })
})

test('Google Chat audience edits retain required project without replaying stored credentials', () => {
  const body = channelDraftBody('google_chat', { audience: 'new-audience' }, stored)
  assert.deepEqual(body, { project_number: '123456', audience: 'new-audience' })
})

test('Google Chat can create inbound-only configuration without service account inputs', () => {
  assert.deepEqual(channelDraftBody('google_chat', { project_number: '123456' }), { project_number: '123456' })
})

for (const credential of ['', ' ', '***', '[REDACTED]', '{"private_key":"***"}']) {
  test('Google Chat refuses blank and masked credential drafts', () => {
    assert.throws(() => channelDraftBody('google_chat', { service_account_json: credential }, stored), ZodError)
  })
}

test('fresh Google Chat credential replacements reach only the canonical REST mapping', async (t) => {
  const provider = CHANNELS.find((item) => item.key === 'google_chat')
  assert.ok(provider)
  const json = JSON.stringify({ client_email: 'synthetic@provider.test', private_key: 'synthetic-replacement' })
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    assert.equal(String(input), `/api/agents/${id}/google-chat-channel`)
    assert.equal(init?.method, 'POST')
    assert.deepEqual(JSON.parse(String(init?.body)), { project_number: '123456', service_account_json: json })
    return Response.json(stored)
  })
  await saveChannel({ agentId: id, path: provider.path }, channelDraftBody('google_chat', { service_account_json: json }, stored))
})

test('Teams uses its canonical catalog identifier with the existing REST route', () => {
  assert.equal(CHANNELS.find((item) => item.key === 'microsoft_teams')?.path, 'teams-channel')
})

test('channel completion after owner replacement is rejected', async (t) => {
  const response = Promise.withResolvers<Response>()
  const owner = authSession.capture()
  t.mock.method(globalThis, 'fetch', () => response.promise)
  const request = getChannel(id, 'google-chat-channel', { owner })
  authSession.adoptStoredSession(owner)
  response.resolve(Response.json(stored))
  await assert.rejects(request, StaleSessionError)
})

test('catalog completion after owner replacement is rejected', async (t) => {
  const response = Promise.withResolvers<Response>()
  const owner = authSession.capture()
  t.mock.method(globalThis, 'fetch', () => response.promise)
  const request = getChannelCatalog({ owner })
  authSession.adoptStoredSession(owner)
  response.resolve(Response.json([capability]))
  await assert.rejects(request, StaleSessionError)
})
