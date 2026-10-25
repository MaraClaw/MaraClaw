import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'

import { invitationHistoryOptions, invitationKeys, revokeInvitationOptions, sendInvitationOptions } from '../src/hooks/invitation-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { invitationIdSchema } from '../src/lib/invitations-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'

const page = { items: [], total: 0, page: 1, page_size: 20 }
const payload = { emails: ['recipient@example.test'], ttl_hours: 336 }

test('disabled and tenantless invitation observers make no HTTP requests', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(page))
  const disabled = new QueryObserver(client, invitationHistoryOptions('tenant-a', 1, false))
  const tenantless = new QueryObserver(client, invitationHistoryOptions(null, 1, true))
  // When
  t.after(disabled.subscribe(() => undefined))
  t.after(tenantless.subscribe(() => undefined))
  // Then
  assert.equal(fetch.mock.callCount(), 0)
})

test('history caches isolate page and tenant', () => {
  // Given / When
  const first = invitationKeys.list('tenant-a', 1)
  // Then
  assert.notDeepEqual(first, invitationKeys.list('tenant-a', 2))
  assert.notDeepEqual(first, invitationKeys.list('tenant-b', 1))
  assert.deepEqual(first.slice(0, 2), invitationKeys.tenant('tenant-a'))
})

for (const kind of ['send', 'revoke'] as const) {
  test(`successful ${kind} invalidates own history without clearing rows or another tenant`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(invitationKeys.list('tenant-a', 1), page)
    client.setQueryData(invitationKeys.list('tenant-a', 2), { ...page, page: 2 })
    client.setQueryData(invitationKeys.list('tenant-b', 1), page)
    t.mock.method(globalThis, 'fetch', async () => kind === 'send' ? Response.json({ invited: 1, message: 'Sent' }) : new Response(null, { status: 204 }))
    // When
    if (kind === 'send') await new MutationObserver(client, ownMutationOptions(policy, policy.capture(), sendInvitationOptions(client, 'tenant-a'))).mutate(payload)
    else await new MutationObserver(client, ownMutationOptions(policy, policy.capture(), revokeInvitationOptions(client, 'tenant-a'))).mutate(invitationIdSchema.parse('record-1'))
    // Then
    assert.deepEqual(client.getQueryData(invitationKeys.list('tenant-a', 1)), page)
    assert.equal(client.getQueryState(invitationKeys.list('tenant-a', 1))?.isInvalidated, true)
    assert.equal(client.getQueryState(invitationKeys.list('tenant-a', 2))?.isInvalidated, true)
    assert.equal(client.getQueryState(invitationKeys.list('tenant-b', 1))?.isInvalidated, false)
  })
}

for (const token of ['token-a', 'token-b']) for (const status of [200, 503]) {
  test(`late send ${status} cannot invalidate or trigger UI callbacks after owner replacement ${token}`, async (t) => {
    // Given
    const { policy } = createFixture()
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(invitationKeys.list('tenant-a', 1), page)
    const response = Promise.withResolvers<Response>()
    const started = Promise.withResolvers<void>()
    const fetch = t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
    const callbacks: string[] = []
    const options = sendInvitationOptions(client, 'tenant-a')
    const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), {
      ...options,
      onSuccess: () => { options.onSuccess(); callbacks.push('success') },
      onError: () => { callbacks.push('error') },
      onSettled: () => { callbacks.push('settled') },
    }))
    const rejected = assert.rejects(mutation.mutate(payload), StaleSessionError)
    await started.promise
    // When
    policy.replace(token)
    response.resolve(Response.json(status === 200 ? { invited: 1, message: 'Sent' } : { detail: 'SMTP disabled' }, { status }))
    // Then
    await rejected
    assert.deepEqual(callbacks, [])
    assert.equal(client.getQueryState(invitationKeys.list('tenant-a', 1))?.isInvalidated, false)
    assert.equal(fetch.mock.callCount(), 1)
  })
}

test('failed sends are not automatically retried and preserve loaded history', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(invitationKeys.list('tenant-a', 1), page)
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'SMTP disabled' }, { status: 400 }))
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), sendInvitationOptions(client, 'tenant-a')))
  // When
  await assert.rejects(mutation.mutate(payload))
  // Then
  assert.equal(fetch.mock.callCount(), 1)
  assert.deepEqual(client.getQueryData(invitationKeys.list('tenant-a', 1)), page)
  assert.equal(client.getQueryState(invitationKeys.list('tenant-a', 1))?.isInvalidated, false)
})
