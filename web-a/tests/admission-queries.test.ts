import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'

import { admissionDecisionOptions, admissionDetailOptions, admissionKeys, admissionReviewsOptions } from '../src/hooks/admission-options.ts'
import { notificationKeys } from '../src/hooks/notification-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { ownQueryClient } from '../src/hooks/session-query-client.ts'
import { admissionDetailSchema } from '../src/lib/admissions-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { approvedAdmission, pendingAdmission } from './admission-fixtures.ts'
import { createFixture } from './helpers.ts'

test('disabled admission list and detail observers make no requests', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
  const list = new QueryObserver(client, admissionReviewsOptions('pending', 'later-page', false))
  const detail = new QueryObserver(client, admissionDetailOptions(pendingAdmission.id, false))
  // When
  t.after(list.subscribe(() => undefined))
  t.after(detail.subscribe(() => undefined))
  // Then
  assert.equal(fetch.mock.callCount(), 0)
  assert.equal(list.getCurrentResult().fetchStatus, 'idle')
  assert.equal(detail.getCurrentResult().fetchStatus, 'idle')
})

test('review page keys isolate cursors and statuses while sharing the list invalidation prefix', () => {
  // Given
  const first = admissionKeys.list('pending', null)
  const next = admissionKeys.list('pending', 'opaque-page')
  // When / Then
  assert.notDeepEqual(first, next)
  assert.notDeepEqual(next, admissionKeys.list('approved', 'opaque-page'))
  assert.deepEqual(first.slice(0, 2), admissionKeys.lists)
  assert.deepEqual(next, ['admin-admission-reviews', 'list', 'pending', 'opaque-page'])
})

test('review polling refetches the same cursor with query cancellation signals', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: 'next' }))
  const options = admissionReviewsOptions('withdrawn', 'current', true)
  // When
  await client.fetchQuery(options)
  await client.fetchQuery(options)
  // Then
  assert.equal(options.refetchInterval, 30_000)
  assert.deepEqual(fetch.mock.calls.map((call) => call.arguments[0]), Array(2).fill('/api/company-admissions/review/page?status=withdrawn&limit=50&cursor=current'))
  assert.ok(fetch.mock.calls.every((call) => call.arguments[1]?.signal instanceof AbortSignal))
})

test('invalid admission links do not issue a detail request', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(pendingAdmission))
  const detail = new QueryObserver(client, admissionDetailOptions(null, true))
  // When
  t.after(detail.subscribe(() => undefined))
  // Then
  assert.equal(fetch.mock.callCount(), 0)
})

test('a successful decision replaces detail and removes it from every cached pending page', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  const other = admissionDetailSchema.parse({ ...pendingAdmission, id: '8bec1dd5-58e8-41f8-a096-46f85551eab2' })
  client.setQueryData(admissionKeys.detail(pendingAdmission.id), pendingAdmission)
  client.setQueryData(admissionKeys.list('pending', null), { items: [pendingAdmission, other], next_cursor: 'second' })
  client.setQueryData(admissionKeys.list('pending', 'second'), { items: [pendingAdmission], next_cursor: 'third' })
  client.setQueryData(admissionKeys.list('pending', 'last'), { items: [pendingAdmission], next_cursor: null })
  client.setQueryData(admissionKeys.list('approved', null), { items: [], next_cursor: null })
  client.setQueryData(notificationKeys.unread, { unread_count: 1 })
  t.mock.method(globalThis, 'fetch', async () => Response.json(approvedAdmission))
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), admissionDecisionOptions(client, pendingAdmission.id)))
  // When
  await mutation.mutate({ decision: 'approved', reason: 'Confirmed membership' })
  // Then
  assert.deepEqual(client.getQueryData(admissionKeys.detail(pendingAdmission.id)), approvedAdmission)
  assert.deepEqual(client.getQueryData(admissionKeys.list('pending', null)), { items: [other], next_cursor: 'second' })
  assert.deepEqual(client.getQueryData(admissionKeys.list('pending', 'second')), { items: [], next_cursor: 'third' })
  assert.deepEqual(client.getQueryData(admissionKeys.list('pending', 'last')), { items: [], next_cursor: null })
  assert.equal(client.getQueryState(admissionKeys.list('approved', null))?.isInvalidated, true)
  assert.equal(client.getQueryState(notificationKeys.unread)?.isInvalidated, true)
})

test('a 409 refreshes active detail and pending list without retrying the decision', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  t.after(() => client.clear())
  client.setQueryData(admissionKeys.detail(pendingAdmission.id), pendingAdmission)
  client.setQueryData(admissionKeys.list('pending', 'current'), { items: [pendingAdmission], next_cursor: 'next' })
  const freshDetail = Promise.withResolvers<void>()
  const freshList = Promise.withResolvers<void>()
  const fetch = t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL, options?: RequestInit) => {
    if (options?.method === 'POST') return Response.json({ detail: 'Already decided' }, { status: 409 })
    return Response.json(String(url).includes('/review/page?') ? { items: [], next_cursor: 'next' } : approvedAdmission)
  })
  const detail = new QueryObserver(client, admissionDetailOptions(pendingAdmission.id, true))
  const list = new QueryObserver(client, admissionReviewsOptions('pending', 'current', true))
  t.after(detail.subscribe((result) => { if (result.data?.status === 'approved') freshDetail.resolve() }))
  t.after(list.subscribe((result) => { if (result.data?.items.length === 0) freshList.resolve() }))
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), admissionDecisionOptions(client, pendingAdmission.id)))
  // When
  await assert.rejects(mutation.mutate({ decision: 'rejected', reason: null }))
  await Promise.all([freshDetail.promise, freshList.promise])
  // Then
  assert.equal(detail.getCurrentResult().data?.status, 'approved')
  assert.deepEqual(list.getCurrentResult().data, { items: [], next_cursor: 'next' })
  assert.equal(fetch.mock.calls.filter((call) => call.arguments[1]?.method === 'POST').length, 1)
  assert.equal(mutation.getCurrentResult().isError, true)
})

test('a 503 decision preserves all admission and notification caches for a deliberate retry', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(admissionKeys.detail(pendingAdmission.id), pendingAdmission)
  client.setQueryData(admissionKeys.list('pending', null), { items: [pendingAdmission], next_cursor: 'next' })
  client.setQueryData(admissionKeys.list('pending', 'next'), { items: [], next_cursor: null })
  client.setQueryData(admissionKeys.list('approved', null), { items: [approvedAdmission], next_cursor: null })
  client.setQueryData(notificationKeys.unread, { unread_count: 2 })
  const before = client.getQueryCache().getAll().map((query) => query.state)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), admissionDecisionOptions(client, pendingAdmission.id)))
  // When
  await assert.rejects(mutation.mutate({ decision: 'approved', reason: null }))
  // Then
  assert.deepEqual(client.getQueryCache().getAll().map((query) => query.state), before)
})

for (const kind of ['detail', 'list'] as const) {
test(`an old ${kind} poll cannot overwrite a successful admission decision`, async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(admissionKeys.detail(pendingAdmission.id), pendingAdmission)
  client.setQueryData(admissionKeys.list('pending', 'current'), { items: [pendingAdmission], next_cursor: 'next' })
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<AbortSignal | null | undefined>()
  t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, options?: RequestInit) => {
    if (options?.method === 'POST') return Response.json(approvedAdmission)
    started.resolve(options?.signal)
    return response.promise
  })
  const poll = kind === 'detail'
    ? client.fetchQuery({ ...admissionDetailOptions(pendingAdmission.id, true), staleTime: 0 })
    : client.fetchQuery({ ...admissionReviewsOptions('pending', 'current', true), staleTime: 0 })
  const signal = await started.promise
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), admissionDecisionOptions(client, pendingAdmission.id)))
  // When
  await mutation.mutate({ decision: 'approved', reason: null })
  response.resolve(Response.json(kind === 'detail' ? pendingAdmission : { items: [pendingAdmission], next_cursor: 'next' }))
  await poll
  // Then
  assert.equal(signal?.aborted, true)
  assert.deepEqual(client.getQueryData(admissionKeys.detail(pendingAdmission.id)), approvedAdmission)
  assert.deepEqual(client.getQueryData(admissionKeys.list('pending', 'current')), { items: [], next_cursor: 'next' })
})
}

test('a retired session decision cannot write review caches or invalidate notifications', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(admissionKeys.detail(pendingAdmission.id), pendingAdmission)
  const page = { items: [pendingAdmission], next_cursor: 'next' }
  client.setQueryData(admissionKeys.list('pending', null), page)
  client.setQueryData(notificationKeys.unread, { unread_count: 2 })
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), admissionDecisionOptions(client, pendingAdmission.id)))
  const rejected = assert.rejects(mutation.mutate({ decision: 'approved', reason: null }), StaleSessionError)
  await started.promise
  // When
  policy.replace('replacement-token')
  response.resolve(Response.json(approvedAdmission))
  await rejected
  // Then
  assert.deepEqual(client.getQueryData(admissionKeys.detail(pendingAdmission.id)), pendingAdmission)
  assert.deepEqual(client.getQueryData(admissionKeys.list('pending', null)), page)
  assert.equal(client.getQueryState(notificationKeys.unread)?.isInvalidated, false)
})

test('a retired session cancels a late review page without populating the replacement cache', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  const replacement = new QueryClient()
  t.after(() => { client.clear(); replacement.clear() })
  t.after(ownQueryClient(policy, policy.capture(), client))
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<AbortSignal | null | undefined>()
  t.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, options?: RequestInit) => {
    started.resolve(options?.signal)
    return response.promise
  })
  const poll = client.fetchQuery(admissionReviewsOptions('pending', 'old-cursor', true))
  const rejected = assert.rejects(poll)
  const signal = await started.promise
  // When
  policy.replace('replacement-token')
  response.resolve(Response.json({ items: [pendingAdmission], next_cursor: 'next' }))
  await rejected
  // Then
  assert.equal(signal?.aborted, true)
  assert.deepEqual(client.getQueryCache().getAll(), [])
  assert.deepEqual(replacement.getQueryCache().getAll(), [])
})
