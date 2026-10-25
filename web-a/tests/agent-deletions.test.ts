import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'
import { z } from 'zod'

import { agentDeletionApprovalOptions, agentDeletionDetailOptions, agentDeletionKeys, agentDeletionListOptions } from '../src/hooks/agent-deletion-options.ts'
import { notificationKeys } from '../src/hooks/notification-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { approveAgentDeletion, canApproveAgentDeletion, fetchAgentDeletionDetail, fetchAgentDeletionRequests } from '../src/lib/agent-deletion-api.ts'
import { agentLifecycleError } from '../src/lib/agent-lifecycle-errors.ts'
import { ApiError } from '../src/lib/http.ts'
import { safeNotificationLink } from '../src/lib/notification-links.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { canReviewAgentDeletions } from '../src/lib/types/auth.ts'
import { approvedDeletion, deletionRequest } from './agent-lifecycle-fixtures.ts'
import { adminUser, createFixture } from './helpers.ts'

test('a deletion list preserves filters and cursor without recording a detail view', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [deletionRequest], next_cursor: 'next' }))
  const signal = new AbortController().signal
  // When
  const page = await fetchAgentDeletionRequests({ status: 'pending', cursor: 'opaque+/=', agentId: deletionRequest.agent_id }, signal)
  // Then
  assert.equal(fetch.mock.callCount(), 1)
  assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/agent-deletion-requests?status=pending&limit=50&cursor=opaque%2B%2F%3D&agent_id=${deletionRequest.agent_id}`)
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal, signal)
  assert.equal(page.items[0]?.id, deletionRequest.id)
  assert.equal('viewer' in (page.items[0] ?? {}), false)
})

test('detail loads metadata and viewed flags through the dedicated GET', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(deletionRequest))
  // When
  const detail = await fetchAgentDeletionDetail(deletionRequest.id)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/agent-deletion-requests/${deletionRequest.id}`)
  assert.deepEqual(detail, deletionRequest)
  assert.equal(Object.isFrozen(detail.viewer), true)
})

test('approval sends an empty POST body and parses counted approval flags', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(approvedDeletion))
  // When
  const result = await approveAgentDeletion(deletionRequest.id)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/agent-deletion-requests/${deletionRequest.id}/approve`)
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.method, 'POST')
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.body, undefined)
  assert.equal(result.approvals[0]?.counted, true)
})

for (const invalid of [
  { ...deletionRequest, viewer: undefined }, { ...deletionRequest, status: 'approved' },
  { ...deletionRequest, progress: { ...deletionRequest.progress, required_approvals: 1 } },
  { ...deletionRequest, approvals: [{ ...approvedDeletion.approvals[0], counted: undefined }] },
  { ...deletionRequest, requester: { ...deletionRequest.requester, display_name: null } },
  { ...deletionRequest, agent: { ...deletionRequest.agent, role_description: null } },
  { ...deletionRequest, approvals: [{ ...approvedDeletion.approvals[0], display_name: null }] },
]) {
  test('malformed deletion details fail at the boundary', async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    // When / Then
    await assert.rejects(fetchAgentDeletionDetail(deletionRequest.id), z.ZodError)
  })
}

test('approval gating requires a successful current detail load and every server viewer flag', () => {
  // Given
  const denied = [
    { ...deletionRequest, status: 'executed' as const }, { ...deletionRequest, status: 'cancelled' as const },
    { ...deletionRequest, viewer: { ...deletionRequest.viewer, can_approve: false } },
    { ...deletionRequest, viewer: { ...deletionRequest.viewer, has_viewed: false } },
    { ...deletionRequest, viewer: { ...deletionRequest.viewer, has_approved: true } },
  ]
  // When / Then
  assert.equal(canApproveAgentDeletion(deletionRequest, true), true)
  assert.equal(canApproveAgentDeletion(deletionRequest, false), false)
  assert.ok(denied.every((detail) => !canApproveAgentDeletion(detail, true)))
})

test('ordinary and genesis own-org admins can review but platform and inactive accounts cannot', () => {
  // Given
  const user = adminUser()
  const denied = [null, { ...user, role: 'member' }, { ...user, is_platform_admin: true }, { ...user, tenant_id: null }, { ...user, tenant_id: undefined }, { ...user, is_active: false }] as const
  // When / Then
  assert.equal(canReviewAgentDeletions(user), true)
  assert.equal(canReviewAgentDeletions({ ...user, is_genesis: true }), true)
  assert.ok(denied.every((viewer) => !canReviewAgentDeletions(viewer)))
})

test('notification links accept exact deletion routes and reject foreign or malformed destinations', () => {
  // Given
  const route = `/agent-deletion-requests/${deletionRequest.id}`
  const invalid = ['https://foreign.test' + route, '/' + route, route + '/approve', route + '/more', route + '?next=//foreign.test', route + '#fragment', route + '\n', '/agent-deletion-requests/not-a-uuid', '/agent-deletion-requests/%2f%2fevil.test', '/agent-deletion-requests/../users', '/agent-deletion-requests\\evil.test']
  // When / Then
  assert.equal(safeNotificationLink(route), route)
  assert.equal(safeNotificationLink('/agent-deletion-requests'), '/agent-deletion-requests')
  assert.ok(invalid.every((link) => safeNotificationLink(link) === null))
})

test('list-only and invalid detail query observers cannot authorize an approval', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [deletionRequest], next_cursor: null }))
  const invalid = new QueryObserver(client, agentDeletionDetailOptions(null, true))
  t.after(invalid.subscribe(() => undefined))
  await client.fetchQuery(agentDeletionListOptions({ status: 'pending', cursor: null }, true))
  const mutation = new MutationObserver(client, agentDeletionApprovalOptions(client, deletionRequest.id))
  // When / Then
  await assert.rejects(mutation.mutate(undefined), (error) => error instanceof ApiError && error.status === 403)
  assert.equal(fetch.mock.callCount(), 1)
})

test('a successful approval refreshes deletion lists, notifications and agent owner counts', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(agentDeletionKeys.detail(deletionRequest.id), deletionRequest)
  const keys = [agentDeletionKeys.list({ status: 'pending', cursor: 'later' }), notificationKeys.unread, ['admin-users', 'org'], ['admin-user', 'source'], ['admin-companies']]
  for (const key of keys) client.setQueryData(key, {})
  t.mock.method(globalThis, 'fetch', async () => Response.json(approvedDeletion))
  const mutation = new MutationObserver(client, agentDeletionApprovalOptions(client, deletionRequest.id))
  // When
  await mutation.mutate(undefined)
  // Then
  assert.deepEqual(client.getQueryData(agentDeletionKeys.detail(deletionRequest.id)), approvedDeletion)
  assert.ok(keys.every((key) => client.getQueryState(key)?.isInvalidated))
})

for (const status of [403, 409]) {
  test(`a structured ${status} approval error refreshes detail without retrying`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(agentDeletionKeys.detail(deletionRequest.id), deletionRequest)
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { code: status === 403 ? 'reviewer_not_eligible' : 'deletion_request_stale' } }, { status }))
    const mutation = new MutationObserver(client, agentDeletionApprovalOptions(client, deletionRequest.id))
    // When / Then
    await assert.rejects(mutation.mutate(undefined), (error) => error instanceof ApiError && error.status === status && agentLifecycleError(error).length > 0)
    assert.equal(fetch.mock.callCount(), 1)
    assert.equal(client.getQueryState(agentDeletionKeys.detail(deletionRequest.id))?.isInvalidated, true)
  })
}

test('a retired session approval cannot update details or notification caches', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => { client.clear(); policy.logout() })
  client.setQueryData(agentDeletionKeys.detail(deletionRequest.id), deletionRequest)
  client.setQueryData(notificationKeys.unread, { unread_count: 1 })
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.after(() => response.resolve(Response.json(approvedDeletion)))
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), agentDeletionApprovalOptions(client, deletionRequest.id)))
  const result = assert.rejects(mutation.mutate(undefined), StaleSessionError)
  await started.promise
  // When
  policy.replace('new-owner')
  response.resolve(Response.json(approvedDeletion))
  await result
  // Then
  assert.deepEqual(client.getQueryData(agentDeletionKeys.detail(deletionRequest.id)), deletionRequest)
  assert.equal(client.getQueryState(notificationKeys.unread)?.isInvalidated, false)
})
