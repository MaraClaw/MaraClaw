import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'

import { ApiError } from '../src/lib/http.ts'
import { scheduleRunKey, scheduleRunOptions } from '../src/lib/schedule-run-query.ts'
import type { ScheduleRunReceipt } from '../src/lib/schedules-api.ts'
import { assertBackedOff, focusRefetch, pageVisibility, pollInterval, throttled } from './poll-fixture.ts'

const target = { agentId: 'agent', scheduleId: 'schedule', runId: 'durable-run' }
const receipt: ScheduleRunReceipt = {
  run_id: 'durable-run', schedule_id: 'schedule', agent_id: 'agent', requester_id: 'requester',
  status: 'pending', created_at: '2026-10-22T06:00:00+00:00', started_at: null, finished_at: null, can_cancel: true,
}
const watching = { cancelling: false, accessLost: false }

function harness(state = watching) {
  const client = new QueryClient()
  const options = scheduleRunOptions(target, state)
  const query = () => client.getQueryCache().build<ScheduleRunReceipt, Error, ScheduleRunReceipt, ReturnType<typeof scheduleRunKey>>(client, options)
  const enabled = () => typeof options.enabled === 'function' ? options.enabled(query()) : options.enabled
  return { client, options, enabled }
}

for (const status of [null, 'pending', 'running', 'success', 'failure', 'cancelled', 'unknown'] as const) {
  const active = status === null || status === 'pending' || status === 'running'
  test(`a receipt ${status ?? 'not yet read'} ${active ? 'polls every 2 s' : 'stops polling'} and refetches on focus only while it polls`, (t) => {
    // Given
    const { client, options, enabled } = harness()
    t.after(() => client.clear())
    if (status) client.setQueryData(scheduleRunKey(target), { ...receipt, status })
    // When / Then
    assert.equal(pollInterval(client, options), active ? 2000 : false)
    assert.equal(enabled(), active)
    assert.equal(focusRefetch(client, options), 'always')
    assert.equal(options.refetchOnReconnect, false)
  })
}

for (const [hold, state] of [['cancelling', { ...watching, cancelling: true }], ['lost access', { ...watching, accessLost: true }]] as const) {
  test(`an active receipt stops polling while ${hold}`, (t) => {
    // Given
    const { client, options, enabled } = harness(state)
    t.after(() => client.clear())
    client.setQueryData(scheduleRunKey(target), receipt)
    // When / Then
    assert.equal(pollInterval(client, options), false)
    assert.equal(enabled(), false)
  })
}

for (const status of [403, 404]) {
  test(`a receipt that answers ${status} is never polled again`, async (t) => {
    // Given
    const { client, options, enabled } = harness()
    t.after(() => client.clear())
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Gone' }, { status }))
    assert.equal(pollInterval(client, options), 2000)
    // When
    await assert.rejects(client.fetchQuery(options), ApiError)
    // Then
    assert.equal(pollInterval(client, options), false)
    assert.equal(enabled(), false)
  })
}

test('a hidden page stops the receipt poll and a visible one restores 2 s', (t) => {
  // Given
  const { client, options } = harness()
  t.after(() => client.clear())
  const page = pageVisibility(t, 'hidden')
  client.setQueryData(scheduleRunKey(target), receipt)
  // When / Then
  assert.equal(pollInterval(client, options), false)
  page.show()
  assert.equal(pollInterval(client, options), 2000)
})

test('an outage backs the receipt poll off with every failed read and the first success restores 2 s', async (t) => {
  // Given
  const { client, options } = harness()
  t.after(() => client.clear())
  let healthy = false
  t.mock.method(globalThis, 'fetch', async () => healthy ? Response.json({ ...receipt, status: 'running', can_cancel: false }) : throttled(503))
  assert.equal(pollInterval(client, options), 2000)
  // When / Then
  for (const ceiling of [4000, 8000, 16_000, 32_000, 60_000, 60_000]) {
    await assert.rejects(client.fetchQuery(options), ApiError)
    assertBackedOff(pollInterval(client, options), ceiling)
  }
  healthy = true
  await client.fetchQuery(options)
  assert.equal(pollInterval(client, options), 2000)
})

test('a Retry-After on the failed receipt read holds the poll back for that long', async (t) => {
  // Given
  const { client, options } = harness()
  t.after(() => client.clear())
  t.mock.method(globalThis, 'fetch', async () => throttled(429, '30'))
  assert.equal(pollInterval(client, options), 2000)
  // When
  await assert.rejects(client.fetchQuery(options), (error: unknown) => error instanceof ApiError && error.retryAfterMs === 30_000)
  // Then
  assert.equal(pollInterval(client, options), 30_000)
})
