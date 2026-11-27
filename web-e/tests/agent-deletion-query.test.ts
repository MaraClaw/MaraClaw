import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'

import { agentDeletionRequestsKey, agentDeletionRequestsOptions } from '../src/lib/agent-deletion-query.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { ApiError } from '../src/lib/http.ts'
import { agentId, deletionDetail, requestId } from './agent-deletion-fixture.ts'
import { assertBackedOff, focusRefetch, pageVisibility, pollInterval, throttled } from './poll-fixture.ts'

for (const filed of [null, requestId]) {
  test(`${filed ? 'a filed deletion request' : 'the pending deletion lookup'} polls every 30 s only in the foreground and refetches on focus`, (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    const page = pageVisibility(t, 'hidden')
    const options = agentDeletionRequestsOptions(authSession.capture(), agentId, filed)
    // When / Then
    assert.deepEqual(options.queryKey, agentDeletionRequestsKey(agentId, filed))
    assert.equal(pollInterval(client, options), false)
    page.show()
    assert.equal(pollInterval(client, options), 30_000)
    assert.equal(focusRefetch(client, options), true)
    assert.equal(options.staleTime, 0)
    assert.equal('refetchIntervalInBackground' in options, false)
  })
}

test('an outage backs the deletion status poll off to the ceiling and the first success restores 30 s', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  let healthy = false
  t.mock.method(globalThis, 'fetch', async () => healthy ? Response.json(deletionDetail) : throttled(503))
  const options = agentDeletionRequestsOptions(authSession.capture(), agentId, requestId)
  assert.equal(pollInterval(client, options), 30_000)
  // When / Then
  for (let failure = 0; failure < 3; failure++) {
    await assert.rejects(client.fetchQuery(options), ApiError)
    assertBackedOff(pollInterval(client, options), 60_000)
  }
  healthy = true
  assert.deepEqual((await client.fetchQuery(options)).items.map((item) => item.id), [requestId])
  assert.equal(pollInterval(client, options), 30_000)
})

test('a Retry-After on the failed deletion lookup holds the poll back at least that long, never past the ceiling', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const retryAfter = ['50', '3600']
  t.mock.method(globalThis, 'fetch', async () => throttled(429, retryAfter.shift()))
  const options = agentDeletionRequestsOptions(authSession.capture(), agentId, null)
  assert.equal(pollInterval(client, options), 30_000)
  // When
  await assert.rejects(client.fetchQuery(options), (error: unknown) => error instanceof ApiError && error.retryAfterMs === 50_000)
  const delay = pollInterval(client, options)
  await assert.rejects(client.fetchQuery(options), (error: unknown) => error instanceof ApiError && error.retryAfterMs === 3_600_000)
  // Then
  assert.ok(typeof delay === 'number' && delay >= 50_000 && delay <= 60_000, String(delay))
  assert.equal(pollInterval(client, options), 60_000)
})
