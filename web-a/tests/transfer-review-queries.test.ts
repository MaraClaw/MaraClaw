import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'

import { notificationKeys } from '../src/hooks/notification-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { ownQueryClient } from '../src/hooks/session-query-client.ts'
import { reviewTenantOptions, transferDecisionOptions, transferDetailOptions, transferReviewKeys, transferReviewsOptions } from '../src/hooks/transfer-review-options.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'
import type { TransferReviewPage } from '../src/lib/transfer-reviews-api.ts'
import { admittedTransfer, pendingTransfer } from './transfer-review-fixtures.ts'

function page(...items: TransferReviewPage['items']): TransferReviewPage {
  return { items, next_cursor: 'next' }
}

test('review pages are keyed by view and cursor', () => {
  // Given / When / Then
  assert.notDeepEqual(transferReviewKeys.list('pending', null), transferReviewKeys.list('recent', null))
  assert.notDeepEqual(transferReviewKeys.list('pending', null), transferReviewKeys.list('pending', 'next'))
  assert.deepEqual(transferReviewKeys.list('pending', 'next').slice(0, 2), transferReviewKeys.lists)
})

test('disabled access, queue and invalid detail observers make no requests', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([]))
  const observers = [new QueryObserver(client, reviewTenantOptions(null, false)), new QueryObserver(client, transferReviewsOptions('pending', null, false)), new QueryObserver(client, transferDetailOptions(null, true))]
  // When
  for (const observer of observers) t.after(observer.subscribe(() => undefined))
  // Then
  assert.equal(fetch.mock.callCount(), 0)
})

test('a denied decision never sends a request even when called directly', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(admittedTransfer))
  const mutation = new MutationObserver(client, transferDecisionOptions(client, pendingTransfer.id, false))
  // When / Then
  await assert.rejects(mutation.mutate({ decision: 'approved' }), (error: unknown) => error instanceof ApiError && error.status === 403)
  assert.equal(fetch.mock.callCount(), 0)
})

test('a successful payment review cancels old polls, leaves the pending pages and replaces the recent row', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(transferReviewKeys.list('pending', null), page(pendingTransfer))
  client.setQueryData(transferReviewKeys.list('recent', null), page(pendingTransfer))
  client.setQueryData(transferReviewKeys.list('pending', 'older'), page(pendingTransfer))
  client.setQueryData(notificationKeys.unread, { unread_count: 1 })
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<AbortSignal | null | undefined>()
  t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, options?: RequestInit) => {
    if (options?.method === 'POST') return Response.json(admittedTransfer)
    started.resolve(options?.signal)
    return response.promise
  })
  const poll = client.fetchQuery(transferReviewsOptions('pending', null, true))
  const signal = await started.promise
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), transferDecisionOptions(client, pendingTransfer.id, true)))
  // When
  await mutation.mutate({ decision: 'approved', reason: null })
  response.resolve(Response.json(page(pendingTransfer)))
  await poll
  // Then
  assert.equal(signal?.aborted, true)
  assert.deepEqual(client.getQueryData(transferReviewKeys.detail(pendingTransfer.id)), admittedTransfer)
  assert.deepEqual(client.getQueryData(transferReviewKeys.list('pending', null)), page())
  assert.deepEqual(client.getQueryData(transferReviewKeys.list('pending', 'older')), page(), 'every cached pending page drops the decided request')
  assert.deepEqual(client.getQueryData(transferReviewKeys.list('recent', null)), page(admittedTransfer))
  assert.equal(client.getQueryState(notificationKeys.unread)?.isInvalidated, true)
})

for (const status of [403, 409, 503]) {
  test(`${status} decision errors are not retried and only conflicts invalidate transfer reads`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(transferReviewKeys.detail(pendingTransfer.id), pendingTransfer)
    client.setQueryData(transferReviewKeys.list('pending', null), page(pendingTransfer))
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Decision unavailable' }, { status }))
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), transferDecisionOptions(client, pendingTransfer.id, true)))
    // When
    await assert.rejects(mutation.mutate({ decision: 'rejected', reason: null }))
    // Then
    assert.equal(fetch.mock.callCount(), 1)
    assert.deepEqual(client.getQueryData(transferReviewKeys.detail(pendingTransfer.id)), pendingTransfer)
    assert.equal(client.getQueryState(transferReviewKeys.list('pending', null))?.isInvalidated, status === 409)
  })
}

test('a retired-session payment decision cannot update or invalidate caches', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(transferReviewKeys.list('pending', null), page(pendingTransfer))
  client.setQueryData(notificationKeys.unread, { unread_count: 2 })
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), transferDecisionOptions(client, pendingTransfer.id, true)))
  const rejected = assert.rejects(mutation.mutate({ decision: 'approved' }), StaleSessionError)
  await started.promise
  // When
  policy.replace('replacement-token')
  response.resolve(Response.json(admittedTransfer))
  await rejected
  // Then
  assert.deepEqual(client.getQueryData(transferReviewKeys.list('pending', null)), page(pendingTransfer))
  assert.equal(client.getQueryData(transferReviewKeys.detail(pendingTransfer.id)), undefined)
  assert.equal(client.getQueryState(notificationKeys.unread)?.isInvalidated, false)
})

test('retiring a session cancels the current-tenant capability lookup', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  t.after(ownQueryClient(policy, policy.capture(), client))
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<AbortSignal | null | undefined>()
  t.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, options?: RequestInit) => { started.resolve(options?.signal); return response.promise })
  const query = client.fetchQuery(reviewTenantOptions(pendingTransfer.source_tenant_id, true))
  const rejected = assert.rejects(query)
  const signal = await started.promise
  // When
  policy.replace('replacement-token')
  response.resolve(Response.json({ id: pendingTransfer.source_tenant_id, is_active: true, is_default_end_user_org: true }))
  await rejected
  // Then
  assert.equal(signal?.aborted, true)
  assert.deepEqual(client.getQueryCache().getAll(), [])
})
