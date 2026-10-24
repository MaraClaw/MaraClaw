import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'
import { z } from 'zod'

import { broadcastEmailStatusOptions } from '../src/hooks/broadcast-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { ownQueryClient } from '../src/hooks/session-query-client.ts'
import { broadcastIdSchema, cancelBroadcastEmail, fetchBroadcastEmailStatus, type BroadcastEmailStatus, type BroadcastId } from '../src/lib/broadcast-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'

const id = broadcastIdSchema.parse('0d75651c-8c6c-4404-9046-50e1b3ff6dd2')
const status = { broadcast_id: id, available: true, queued: 11, claimed: 7, smtp_accepted: 5, failed: 3, cancelled: 2 }
const cancellation = { ...status, queued: 0, cancelled: 13, cancelled_now: 11 }

test('email status uses a tenant-local GET and forwards cancellation to the transport', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(status))
  // When
  const result = await fetchBroadcastEmailStatus(id, controller.signal)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/notifications/broadcast/${id}/email-status`)
  assert.equal(options?.method, 'GET')
  assert.equal(options?.signal, controller.signal)
  assert.deepEqual(result, status)
  assert.equal(Object.isFrozen(result), true)
})

test('cancellation posts only the broadcast ID and retains distinct aggregate and newly-cancelled counts', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(cancellation))
  // When
  const result = await cancelBroadcastEmail(id)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `/api/notifications/broadcast/${id}/cancel-email`)
  assert.equal(options?.method, 'POST')
  assert.equal(options?.body, undefined)
  assert.deepEqual(result, cancellation)
  assert.equal(Object.isFrozen(result), true)
})

for (const field of ['queued', 'claimed', 'smtp_accepted', 'failed', 'cancelled', 'cancelled_now']) {
  for (const value of [-1, 1.5, '2', null, undefined]) test(`cancellation rejects ${field}=${value}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ ...cancellation, [field]: value }))
    // When / Then
    await assert.rejects(cancelBroadcastEmail(id), z.ZodError)
  })
}

for (const call of [fetchBroadcastEmailStatus, cancelBroadcastEmail]) {
  for (const malformed of [null, {}, { ...cancellation, available: 'true' }, { ...cancellation, broadcast_id: 'not-a-uuid' }, { ...cancellation, broadcast_id: '0d75651c-8c6c-4404-9046-50e1b3ff6dd3' }]) {
    test(`${call.name} rejects malformed or unrelated receipts ${JSON.stringify(malformed)}`, async (t) => {
      // Given
      t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
      // When / Then
      await assert.rejects(call(id), z.ZodError)
    })
  }
  test(`${call.name} accepts unavailable and all-zero counts without recipient details`, async (t) => {
    // Given
    const empty = { broadcast_id: id, available: false, queued: 0, claimed: 0, smtp_accepted: 0, failed: 0, cancelled: 0, cancelled_now: 0 }
    t.mock.method(globalThis, 'fetch', async () => Response.json(empty))
    // When
    const result = await call(id)
    // Then
    assert.equal(result.available, false)
    assert.equal(result.queued + result.claimed + result.smtp_accepted + result.failed + result.cancelled, 0)
  })
  test(`${call.name} preserves HTTP failure without retrying`, async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Synthetic denied' }, { status: 403 }))
    // When / Then
    await assert.rejects(call(id), (error) => error instanceof ApiError && error.status === 403)
    assert.equal(fetch.mock.callCount(), 1)
  })
}

for (const [changes, expected] of [
  [{ queued: 0, claimed: 1 }, 5_000], [{ queued: 1, claimed: 0 }, 5_000],
  [{ queued: 0, claimed: 0 }, false], [{ available: false }, false],
] as const) test(`polling follows meaningful work ${JSON.stringify(changes)}`, (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const options = broadcastEmailStatusOptions(id)
  const query = client.getQueryCache().build<BroadcastEmailStatus, Error, BroadcastEmailStatus, readonly ['admin-broadcast-email', BroadcastId]>(client, options)
  const interval = options.refetchInterval
  assert.ok(typeof interval === 'function')
  query.setData({ ...status, ...changes })
  // When / Then
  assert.equal(interval(query), expected)
  query.setState({ status: 'error' })
  assert.equal(interval(query), false)
})

test('unmounting the status observer aborts a pending response and does not cache its late counts', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<AbortSignal | null | undefined>()
  t.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, options?: RequestInit) => { started.resolve(options?.signal); return response.promise })
  const options = broadcastEmailStatusOptions(id)
  const observer = new QueryObserver(client, options)
  const unlisten = observer.subscribe(() => undefined)
  const signal = await started.promise
  // When
  unlisten()
  response.resolve(Response.json(status))
  await response.promise
  // Then
  assert.equal(signal?.aborted, true)
  assert.equal(client.getQueryData(options.queryKey), undefined)
})

test('retiring a query owner removes cached status and ignores delayed responses', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  t.after(ownQueryClient(policy, policy.capture(), client))
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const options = broadcastEmailStatusOptions(id)
  const pending = client.fetchQuery(options)
  const rejected = assert.rejects(pending)
  await started.promise
  // When
  policy.replace('new-owner')
  response.resolve(Response.json(status))
  // Then
  await rejected
  assert.equal(client.getQueryData(options.queryKey), undefined)
})

for (const token of ['token-a', 'token-b']) for (const code of [200, 503]) {
  test(`late cancel ${code} cannot run callbacks after owner replacement ${token}`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const callbacks: string[] = []
    const observer = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), {
      mutationFn: cancelBroadcastEmail, retry: false,
      onSuccess: () => { callbacks.push('counts') }, onError: () => { callbacks.push('error') }, onSettled: () => { callbacks.push('refresh') },
    }))
    const rejected = assert.rejects(observer.mutate(id), StaleSessionError)
    await started.promise
    // When
    policy.replace(token)
    response.resolve(Response.json(code === 200 ? cancellation : { detail: 'Synthetic failure' }, { status: code }))
    // Then
    await rejected
    assert.deepEqual(callbacks, [])
  })
}
