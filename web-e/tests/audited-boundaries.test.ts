import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'
import { fetchCurrentUser, loginRequest } from '../src/lib/auth-api.ts'
import { controlScreenshot } from '../src/lib/control-api.ts'
import { getOkrSettings, listObjectives } from '../src/lib/okr-api.ts'

const id = '11111111-1111-4111-8111-111111111111'
const user = {
  id, email: 'member@example.test', display_name: 'Member', tenant_id: id,
  role: 'unknown-contract-value', email_verified: true,
  created_at: '2026-10-25T08:00:00Z', is_active: true,
}

test('current-user parsing accepts the canonical member role without weakening other roles', async (t) => {
  // Given
  const member = { ...user, role: 'member' }
  t.mock.method(globalThis, 'fetch', async () => Response.json(member))
  // When
  const result = await fetchCurrentUser('token')
  // Then
  assert.deepEqual(result, member)
})

test('current-user parsing keeps member defaults for omitted optional profile fields', async (t) => {
  // Given
  const member = { id, display_name: 'Member', role: 'member', created_at: user.created_at, is_active: true }
  t.mock.method(globalThis, 'fetch', async () => Response.json(member))
  // When
  const result = await fetchCurrentUser('token')
  // Then
  assert.deepEqual(result, { ...member, email: null, tenant_id: null, email_verified: false })
})

test('current-user parsing retains explicit nullable member profile fields', async (t) => {
  // Given
  const member = { ...user, role: 'member', email: null, tenant_id: null }
  t.mock.method(globalThis, 'fetch', async () => Response.json(member))
  // When
  const result = await fetchCurrentUser('token')
  // Then
  assert.deepEqual(result, member)
})

test('current-user responses reject an unsupported authorization role', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json(user))
  // When / Then
  await assert.rejects(fetchCurrentUser('token'), ZodError)
})

test('login parses the nested user role before a session can be installed', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ access_token: 'token', user }))
  // When / Then
  await assert.rejects(loginRequest({ login_identifier: 'member@example.test', password: 'password' }), ZodError)
})

test('control screenshots reject an unknown action result status', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'unknown-contract-value', screenshot: null }))
  // When / Then
  await assert.rejects(controlScreenshot({ agentId: id, sessionId: id, ownerToken: 'lease-owner' }), ZodError)
})

test('control screenshots require an action result status', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ screenshot: null }))
  // When / Then
  await assert.rejects(controlScreenshot({ agentId: id, sessionId: id, ownerToken: 'lease-owner' }), ZodError)
})

for (const screenSize of [null, {}, { width: 1280 }, { height: 720 }]) {
  test(`control screenshots accept nullable or partial screen dimensions ${JSON.stringify(screenSize)} with their lease`, async (t) => {
    // Given
    const response = { status: 'ok', screenshot: null, screen_size: screenSize }
    const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) => Response.json(response))
    // When
    const result = await controlScreenshot({ agentId: id, sessionId: id, ownerToken: 'lease-owner' })
    // Then
    assert.deepEqual(result, response)
    const [url, init] = fetch.mock.calls[0]?.arguments ?? []
    assert.equal(url, `/api/agents/${id}/control/screenshot`)
    assert.deepEqual(JSON.parse(String(init?.body)), { session_id: id, owner_token: 'lease-owner' })
  })
}

test('OKR settings reject an unsupported period frequency', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({
    enabled: true, daily_report_enabled: true, daily_report_time: '18:00',
    weekly_report_enabled: true, weekly_report_day: 5, period_frequency: 'unknown-contract-value',
  }))
  // When / Then
  await assert.rejects(getOkrSettings(), ZodError)
})

for (const frequency of ['monthly', 'quarterly', 'custom']) {
  test(`OKR settings preserve the supported ${frequency} period frequency`, async (t) => {
    // Given
    const response = {
      enabled: true, daily_report_enabled: true, daily_report_time: '18:00',
      weekly_report_enabled: false, weekly_report_day: 0, period_frequency: frequency, period_length_days: 45,
    }
    t.mock.method(globalThis, 'fetch', async () => Response.json(response))
    // When
    const result = await getOkrSettings()
    // Then
    assert.deepEqual(result, response)
  })
}

test('OKR objectives parse nested key-result statuses rather than trusting a DTO', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json([{
    id, title: 'Objective', owner_type: 'company', period_start: '2026-10-01', period_end: '2026-12-31',
    status: 'active', created_at: '2026-10-25T08:00:00Z', key_results: [{
      id, objective_id: id, title: 'Result', target_value: 100, current_value: 0,
      status: 'unknown-contract-value', last_updated_at: '2026-10-25T08:00:00Z', created_at: '2026-10-25T08:00:00Z',
    }],
  }]))
  // When / Then
  await assert.rejects(listObjectives(), ZodError)
})
