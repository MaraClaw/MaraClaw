import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'

import { admissionDecisionOptions, admissionDetailOptions, admissionKeys, admissionReviewsOptions } from '../src/hooks/admission-options.ts'
import { notificationKeys } from '../src/hooks/notification-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { admissionDetailSchema } from '../src/lib/admissions-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { approvedAdmission, pendingAdmission } from './admission-fixtures.ts'
import { createFixture } from './helpers.ts'

test('disabled admission list and detail observers make no requests', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([]))
  const list = new QueryObserver(client, admissionReviewsOptions('pending', 0, false))
  const detail = new QueryObserver(client, admissionDetailOptions(pendingAdmission.id, false))
  // When
  t.after(list.subscribe(() => undefined))
  t.after(detail.subscribe(() => undefined))
  // Then
  assert.equal(fetch.mock.callCount(), 0)
  assert.equal(list.getCurrentResult().fetchStatus, 'idle')
  assert.equal(detail.getCurrentResult().fetchStatus, 'idle')
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
  client.setQueryData(admissionKeys.list('pending', 0), [pendingAdmission, other])
  client.setQueryData(admissionKeys.list('pending', 50), [pendingAdmission])
  client.setQueryData(admissionKeys.list('approved', 0), [])
  client.setQueryData(notificationKeys.unread, { unread_count: 1 })
  t.mock.method(globalThis, 'fetch', async () => Response.json(approvedAdmission))
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), admissionDecisionOptions(client, pendingAdmission.id)))
  // When
  await mutation.mutate({ decision: 'approved', reason: 'Confirmed membership' })
  // Then
  assert.deepEqual(client.getQueryData(admissionKeys.detail(pendingAdmission.id)), approvedAdmission)
  assert.deepEqual(client.getQueryData(admissionKeys.list('pending', 0)), [other])
  assert.deepEqual(client.getQueryData(admissionKeys.list('pending', 50)), [])
  assert.equal(client.getQueryState(admissionKeys.list('approved', 0))?.isInvalidated, true)
  assert.equal(client.getQueryState(notificationKeys.unread)?.isInvalidated, true)
})

test('a 409 refreshes active detail and pending list without retrying the decision', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  t.after(() => client.clear())
  client.setQueryData(admissionKeys.detail(pendingAdmission.id), pendingAdmission)
  client.setQueryData(admissionKeys.list('pending', 0), [pendingAdmission])
  const freshDetail = Promise.withResolvers<void>()
  const freshList = Promise.withResolvers<void>()
  const fetch = t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL, options?: RequestInit) => {
    if (options?.method === 'POST') return Response.json({ detail: 'Already decided' }, { status: 409 })
    return Response.json(String(url).includes('/review?') ? [] : approvedAdmission)
  })
  const detail = new QueryObserver(client, admissionDetailOptions(pendingAdmission.id, true))
  const list = new QueryObserver(client, admissionReviewsOptions('pending', 0, true))
  t.after(detail.subscribe((result) => { if (result.data?.status === 'approved') freshDetail.resolve() }))
  t.after(list.subscribe((result) => { if (result.data?.length === 0) freshList.resolve() }))
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), admissionDecisionOptions(client, pendingAdmission.id)))
  // When
  await assert.rejects(mutation.mutate({ decision: 'rejected', reason: null }))
  await Promise.all([freshDetail.promise, freshList.promise])
  // Then
  assert.equal(detail.getCurrentResult().data?.status, 'approved')
  assert.deepEqual(list.getCurrentResult().data, [])
  assert.equal(fetch.mock.calls.filter((call) => call.arguments[1]?.method === 'POST').length, 1)
  assert.equal(mutation.getCurrentResult().isError, true)
})

test('a failed decision preserves the pending detail for a deliberate retry', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(admissionKeys.detail(pendingAdmission.id), pendingAdmission)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), admissionDecisionOptions(client, pendingAdmission.id)))
  // When
  await assert.rejects(mutation.mutate({ decision: 'approved', reason: null }))
  // Then
  assert.deepEqual(client.getQueryData(admissionKeys.detail(pendingAdmission.id)), pendingAdmission)
  assert.equal(client.getQueryState(admissionKeys.detail(pendingAdmission.id))?.isInvalidated, false)
})

test('an old detail poll cannot overwrite a successful admission decision', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(admissionKeys.detail(pendingAdmission.id), pendingAdmission)
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<AbortSignal | null | undefined>()
  t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, options?: RequestInit) => {
    if (options?.method === 'POST') return Response.json(approvedAdmission)
    started.resolve(options?.signal)
    return response.promise
  })
  const poll = client.fetchQuery({ ...admissionDetailOptions(pendingAdmission.id, true), staleTime: 0 })
  const signal = await started.promise
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), admissionDecisionOptions(client, pendingAdmission.id)))
  // When
  await mutation.mutate({ decision: 'approved', reason: null })
  response.resolve(Response.json(pendingAdmission))
  await poll
  // Then
  assert.equal(signal?.aborted, true)
  assert.deepEqual(client.getQueryData(admissionKeys.detail(pendingAdmission.id)), approvedAdmission)
})

test('a retired session decision cannot write review caches or invalidate notifications', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(admissionKeys.detail(pendingAdmission.id), pendingAdmission)
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
  assert.equal(client.getQueryState(notificationKeys.unread)?.isInvalidated, false)
})
