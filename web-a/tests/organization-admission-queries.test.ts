import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'

import { notificationKeys } from '../src/hooks/notification-options.ts'
import { organizationAdmissionDecisionOptions, organizationAdmissionEmailRetryOptions, organizationAdmissionKeys, organizationAdmissionsOptions } from '../src/hooks/organization-admission-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'
import { approvedOrganizationAdmission, organizationAdmission } from './organization-admission-fixtures.ts'

test('disabled organization queue observers do not send requests', (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], has_more: false }))
  const observer = new QueryObserver(client, organizationAdmissionsOptions('pending', 0, false))
  t.after(observer.subscribe(() => undefined))
  assert.equal(fetch.mock.callCount(), 0)
})

test('organization queue caches isolate status and offset', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], has_more: false }))
  await client.fetchQuery(organizationAdmissionsOptions('pending', 50, true))
  assert.deepEqual(client.getQueryData(organizationAdmissionKeys.list('pending', 50)), { items: [], has_more: false })
  assert.equal(client.getQueryData(organizationAdmissionKeys.list('pending', 0)), undefined)
  assert.equal(client.getQueryData(organizationAdmissionKeys.list('approved', 50)), undefined)
})

test('disallowed organization decisions reject before any POST', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(approvedOrganizationAdmission))
  const mutation = new MutationObserver(client, organizationAdmissionDecisionOptions(client, organizationAdmission.id, false))
  await assert.rejects(mutation.mutate({ decision: 'approve' }), (error) => error instanceof ApiError && error.status === 403)
  assert.equal(fetch.mock.callCount(), 0)
})

test('organization decisions replace cached records and invalidate queues and notifications', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const pageKey = organizationAdmissionKeys.list('pending', 0)
  client.setQueryData(pageKey, { items: [organizationAdmission], has_more: false })
  client.setQueryData(notificationKeys.unread, { unread_count: 1 })
  t.mock.method(globalThis, 'fetch', async () => Response.json(approvedOrganizationAdmission))
  const mutation = new MutationObserver(client, organizationAdmissionDecisionOptions(client, organizationAdmission.id, true))
  await mutation.mutate({ decision: 'approve' })
  assert.deepEqual(client.getQueryData(pageKey), { items: [approvedOrganizationAdmission], has_more: false })
  assert.equal(client.getQueryState(pageKey)?.isInvalidated, true)
  assert.equal(client.getQueryState(notificationKeys.unread)?.isInvalidated, true)
})

test('organization conflicts invalidate the queue without replaying a decision', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const pageKey = organizationAdmissionKeys.list('pending', 0)
  client.setQueryData(pageKey, { items: [organizationAdmission], has_more: false })
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Already reviewed' }, { status: 409 }))
  const mutation = new MutationObserver(client, organizationAdmissionDecisionOptions(client, organizationAdmission.id, true))
  await assert.rejects(mutation.mutate({ decision: 'reject' }), (error) => error instanceof ApiError && error.status === 409)
  assert.equal(client.getQueryState(pageKey)?.isInvalidated, true)
  assert.equal(fetch.mock.callCount(), 1)
})

for (const status of [200, 401, 503]) {
  test(`retired organization decision completion (${status}) has no cache or callback effects`, async (t) => {
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    const key = organizationAdmissionKeys.list('pending', 0)
    const page = { items: [organizationAdmission], has_more: false }
    client.setQueryData(key, page)
    client.setQueryData(notificationKeys.unread, { unread_count: 1 })
    const started = Promise.withResolvers<void>()
    const response = Promise.withResolvers<Response>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const callbacks: string[] = []
    const options = organizationAdmissionDecisionOptions(client, organizationAdmission.id, true)
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), {
      ...options, onSuccess: (detail) => { options.onSuccess(detail); callbacks.push('success') },
      onError: (error: Error) => { options.onError(error); callbacks.push('error') },
    }))
    const rejected = assert.rejects(mutation.mutate({ decision: 'approve' }), StaleSessionError)
    await started.promise
    policy.replace('replacement')
    response.resolve(Response.json(status === 200 ? approvedOrganizationAdmission : { detail: 'Late failure' }, { status }))
    await rejected
    assert.deepEqual(client.getQueryData(key), page)
    assert.equal(client.getQueryState(key)?.isInvalidated, false)
    assert.equal(client.getQueryState(notificationKeys.unread)?.isInvalidated, false)
    assert.deepEqual(callbacks, [])
  })
}

test('disallowed credentials retry rejects before transport', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(approvedOrganizationAdmission))
  const mutation = new MutationObserver(client, organizationAdmissionEmailRetryOptions(client, organizationAdmission.id, false))
  await assert.rejects(mutation.mutate(), (error) => error instanceof ApiError && error.status === 403)
  assert.equal(fetch.mock.callCount(), 0)
})

test('credentials retry updates delivery while retaining the approved decision and identity', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const key = organizationAdmissionKeys.list('approved', 0)
  const failed = { ...approvedOrganizationAdmission, credential_email_status: 'failed', credential_email_error: 'smtp_unavailable' }
  client.setQueryData(key, { items: [failed], has_more: false })
  t.mock.method(globalThis, 'fetch', async () => Response.json(approvedOrganizationAdmission))
  const mutation = new MutationObserver(client, organizationAdmissionEmailRetryOptions(client, failed.id, true))
  await mutation.mutate()
  assert.deepEqual(client.getQueryData(key), { items: [approvedOrganizationAdmission], has_more: false })
  assert.equal(client.getQueryState(key)?.isInvalidated, true)
})

for (const status of [409, 503]) {
  test(`credentials retry failure (${status}) retains the approved record without replay`, async (t) => {
    const client = new QueryClient()
    t.after(() => client.clear())
    const key = organizationAdmissionKeys.list('approved', 0), page = { items: [approvedOrganizationAdmission], has_more: false }
    client.setQueryData(key, page)
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Email retry refused' }, { status }))
    const mutation = new MutationObserver(client, organizationAdmissionEmailRetryOptions(client, organizationAdmission.id, true))
    await assert.rejects(mutation.mutate(), (error) => error instanceof ApiError && error.message === 'Email retry refused')
    assert.deepEqual(client.getQueryData(key), page)
    assert.equal(client.getQueryState(key)?.isInvalidated, status === 409)
    assert.equal(fetch.mock.callCount(), 1)
  })
}

for (const status of [200, 401, 503]) {
  test(`retired credentials retry (${status}) has no cache or callback effects`, async (t) => {
    const { policy } = createFixture(), client = new QueryClient()
    t.after(() => client.clear())
    const key = organizationAdmissionKeys.list('approved', 0), page = { items: [approvedOrganizationAdmission], has_more: false }
    client.setQueryData(key, page)
    const started = Promise.withResolvers<void>(), response = Promise.withResolvers<Response>()
    t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const callbacks: string[] = [], options = organizationAdmissionEmailRetryOptions(client, organizationAdmission.id, true)
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), {
      ...options, onSuccess: (detail) => { options.onSuccess(detail); callbacks.push('success') },
      onError: (error: Error) => { options.onError(error); callbacks.push('error') },
    }))
    const rejected = assert.rejects(mutation.mutate(undefined), StaleSessionError)
    await started.promise
    policy.replace('replacement')
    response.resolve(Response.json(status === 200 ? approvedOrganizationAdmission : { detail: 'Late failure' }, { status }))
    await rejected
    assert.deepEqual(client.getQueryData(key), page)
    assert.equal(client.getQueryState(key)?.isInvalidated, false)
    assert.deepEqual(callbacks, [])
  })
}
