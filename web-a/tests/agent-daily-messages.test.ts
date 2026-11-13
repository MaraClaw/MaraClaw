import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { agentDailyMessagesResetOptions } from '../src/hooks/agent-daily-messages-options.ts'
import { canResetAgentDailyMessages } from '../src/lib/agent-daily-messages.ts'
import { ApiError } from '../src/lib/http.ts'
import { resetAgentDailyMessages, type UserAgent, type UserDetail } from '../src/lib/users-api.ts'
import { agentId, sourceId, sourceMember, targetId, tenantId } from './agent-lifecycle-fixtures.ts'
import { adminUser } from './helpers.ts'

const capped = { used: 100, limit: 100, timezone: 'Asia/Ho_Chi_Minh', resets_at: '2026-11-14T00:00:00+07:00' }
const afterReset = { ...capped, used: 0 }
const agent = (id: string, daily: UserAgent['daily_messages']): UserAgent => ({
  id, name: `Agent ${id.slice(0, 4)}`, status: 'running', is_expired: false, is_system: false,
  role_description: null, last_active_at: null, daily_messages: daily,
})
const detail: UserDetail = { ...sourceMember, agents: [agent(agentId, capped), agent(targetId, { ...capped, used: 5 })] }
const detailKey = ['admin-user', sourceId]

test('reset posts no body to the agent route and returns the parsed daily messages', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ ...afterReset, extra: 'ignored' }))
  // When
  const result = await resetAgentDailyMessages(sourceId, agentId)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/users/${sourceId}/agents/${agentId}/daily-messages/reset`)
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.method, 'POST')
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.body, undefined)
  assert.deepEqual(result, afterReset)
})

test('a malformed reset response is rejected instead of rendered', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ used: '0', limit: 100 }))
  // When / Then
  await assert.rejects(resetAgentDailyMessages(sourceId, agentId), z.ZodError)
})

test('only platform admins and same-organization org admins can reset', () => {
  // Given
  const orgAdmin = { ...adminUser(), tenant_id: tenantId }
  // When / Then
  assert.equal(canResetAgentDailyMessages(orgAdmin, sourceMember), true)
  assert.equal(canResetAgentDailyMessages({ ...orgAdmin, role: 'platform_admin', tenant_id: null }, sourceMember), true)
  assert.equal(canResetAgentDailyMessages(orgAdmin, { ...sourceMember, tenant_id: 'foreign' }), false)
  assert.equal(canResetAgentDailyMessages({ ...orgAdmin, tenant_id: null }, { ...sourceMember, tenant_id: null }), false)
  assert.equal(canResetAgentDailyMessages({ ...orgAdmin, role: 'member' }, sourceMember), false)
  assert.equal(canResetAgentDailyMessages(null, sourceMember), false)
})

test('a successful reset shows the returned count for that agent only and refreshes the detail', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(detailKey, detail)
  t.mock.method(globalThis, 'fetch', async () => Response.json(afterReset))
  const mutation = new MutationObserver(client, agentDailyMessagesResetOptions(client, sourceId, agentId))
  // When
  await mutation.mutate(undefined)
  // Then
  const cached = client.getQueryData<UserDetail>(detailKey)
  assert.deepEqual(cached?.agents.map((row) => row.daily_messages), [afterReset, { ...capped, used: 5 }])
  assert.equal(client.getQueryState(detailKey)?.isInvalidated, true)
})

for (const status of [403, 404, 409]) {
  test(`a ${status} reset refreshes the detail and is not retried`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(detailKey, detail)
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'refused' }, { status }))
    const mutation = new MutationObserver(client, agentDailyMessagesResetOptions(client, sourceId, agentId))
    // When / Then
    await assert.rejects(mutation.mutate(undefined), (error) => error instanceof ApiError && error.status === status)
    assert.equal(fetch.mock.callCount(), 1)
    assert.deepEqual(client.getQueryData<UserDetail>(detailKey), detail)
    assert.equal(client.getQueryState(detailKey)?.isInvalidated, true)
  })
}

test('a server failure leaves the cached detail untouched', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(detailKey, detail)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'down' }, { status: 503 }))
  const mutation = new MutationObserver(client, agentDailyMessagesResetOptions(client, sourceId, agentId))
  // When / Then
  await assert.rejects(mutation.mutate(undefined), (error) => error instanceof ApiError && error.status === 503)
  assert.equal(client.getQueryState(detailKey)?.isInvalidated, false)
})
