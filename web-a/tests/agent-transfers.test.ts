import assert from 'node:assert/strict'
import test from 'node:test'
import { InfiniteQueryObserver, MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { agentDeletionKeys } from '../src/hooks/agent-deletion-options.ts'
import { agentTransferKeys, agentTransferOptions, transferTargetsOptions } from '../src/hooks/agent-transfer-options.ts'
import { notificationKeys } from '../src/hooks/notification-options.ts'
import { ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { agentTransferSchema, canTransferMemberAgents, fetchAgentTransferTargets, fetchTransferSource, transferableAgents, transferMemberAgents } from '../src/lib/agent-transfers-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { agentId, deletionRequest, sourceId, sourceMember, targetId, targetMember, tenantId } from './agent-lifecycle-fixtures.ts'
import { adminUser, createFixture } from './helpers.ts'

const scope = { tenantId, sourceUserId: sourceId }
const input = { from_user_id: sourceId, to_user_id: targetId, agent_ids: [agentId] }
const response = { transferred: [agentId], cancelled_deletion_request_ids: [deletionRequest.id] }

test('same-org org admins can transfer before or after source deactivation', () => {
  // Given
  const viewer = { ...adminUser(), tenant_id: tenantId }
  const sources = [sourceMember, { ...sourceMember, is_active: true }, { ...sourceMember, role: 'agent_admin' }]
  // When
  const permissions = sources.map((source) => canTransferMemberAgents(viewer, source))
  // Then
  assert.deepEqual(permissions, [true, true, true])
})

test('platform admins, foreign organizations, and org-admin sources never get the transfer action', () => {
  // Given
  const viewer = { ...adminUser(), tenant_id: tenantId }
  // When / Then
  assert.equal(canTransferMemberAgents({ ...viewer, is_platform_admin: true }, sourceMember), false)
  assert.equal(canTransferMemberAgents({ ...viewer, role: 'platform_admin' }, sourceMember), false)
  assert.equal(canTransferMemberAgents(viewer, { ...sourceMember, tenant_id: 'foreign' }), false)
  assert.equal(canTransferMemberAgents(viewer, { ...sourceMember, role: 'org_admin' }), false)
  assert.equal(canTransferMemberAgents({ ...viewer, is_active: false }, sourceMember), false)
})

test('recipient loading requests one server-filtered page with literal search and opaque cursor', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [targetMember], next_cursor: 'next+/=' }))
  // When
  const result = await fetchAgentTransferTargets({ ...scope, search: ' %_Case+? ', cursor: 'cursor+/=' }, controller.signal)
  // Then
  assert.equal(fetch.mock.callCount(), 1)
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.deepEqual(Object.fromEntries(new URL(String(url), 'https://example.test').searchParams), {
    tenant_id: tenantId, search: ' %_Case+? ', limit: '50', cursor: 'cursor+/=', eligible_for: 'agent_transfer', exclude_user_id: sourceId,
  })
  assert.equal(init?.signal, controller.signal)
  assert.deepEqual(result, { items: [targetMember], next_cursor: 'next+/=' })
})

test('aborting a recipient page propagates cancellation without fetching another page', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async () => { controller.abort(); return Response.json({ items: [], next_cursor: 'later' }) })
  // When / Then
  await assert.rejects(fetchAgentTransferTargets({ ...scope, search: '', cursor: null }, controller.signal), (error) => error instanceof Error && error.name === 'AbortError')
  assert.equal(fetch.mock.callCount(), 1)
})

test('recipient infinite options expose the first page and request each next page only on demand', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const cursors: (string | null)[] = []
  t.mock.method(globalThis, 'fetch', async (url: RequestInfo | URL) => {
    const cursor = new URL(String(url), 'https://example.test').searchParams.get('cursor')
    cursors.push(cursor)
    return Response.json({ items: [{ ...targetMember, id: cursor ? agentId : targetId }], next_cursor: cursor ? null : 'next+/=' })
  })
  const observer = new InfiniteQueryObserver(client, transferTargetsOptions(scope, '', true))
  t.after(observer.subscribe(() => undefined))
  const first = await observer.refetch()
  assert.deepEqual(cursors, [null])
  assert.deepEqual(first.data?.pages.flatMap((page) => page.items.map((member) => member.id)), [targetId])
  assert.equal(observer.getCurrentResult().hasNextPage, true)
  const next = await observer.fetchNextPage()
  assert.deepEqual(cursors, [null, 'next+/='])
  assert.deepEqual(next.data?.pages.flatMap((page) => page.items.map((member) => member.id)), [targetId, agentId])
  assert.equal(next.hasNextPage, false)
})

test('changing recipient search discards previous pages and cursors', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  client.setQueryData(transferTargetsOptions(scope, '', true).queryKey, { pages: [{ items: [targetMember], next_cursor: 'old' }], pageParams: [null] })
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
  const observer = new InfiniteQueryObserver(client, transferTargetsOptions(scope, 'other', true))
  assert.equal(observer.getCurrentResult().data, undefined)
  const result = await observer.refetch()
  const request = new URL(String(fetch.mock.calls[0]?.arguments[0]), 'https://example.test')
  assert.equal(request.searchParams.get('search'), 'other')
  assert.equal(request.searchParams.has('cursor'), false)
  assert.deepEqual(result.data?.pages, [{ items: [], next_cursor: null }])
})

test('a failed next recipient page preserves the first page and permits an explicit retry', async (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => {
    calls += 1
    return calls === 2 ? Response.json({ detail: 'Unavailable' }, { status: 503 })
      : Response.json({ items: calls === 1 ? [targetMember] : [], next_cursor: calls === 1 ? 'next' : null })
  })
  const observer = new InfiniteQueryObserver(client, transferTargetsOptions(scope, '', true))
  t.after(observer.subscribe(() => undefined))
  await observer.refetch()
  const failed = await observer.fetchNextPage()
  assert.equal(calls, 2)
  assert.equal(failed.isFetchNextPageError, true)
  assert.ok(failed.error instanceof ApiError && failed.error.status === 503)
  assert.deepEqual(failed.data?.pages[0]?.items, [targetMember])
  const retry = await observer.fetchNextPage()
  assert.equal(calls, 3)
  assert.equal(retry.isError, false)
  assert.equal(retry.hasNextPage, false)
})

test('disabled recipient options do not request a page', (t) => {
  const client = new QueryClient()
  t.after(() => client.clear())
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], next_cursor: null }))
  const observer = new InfiniteQueryObserver(client, transferTargetsOptions(scope, '', false))
  t.after(observer.subscribe(() => undefined))
  assert.equal(observer.getCurrentResult().fetchStatus, 'idle')
  assert.equal(fetch.mock.callCount(), 0)
})

test('source selection uses owned UserDetail agents and excludes system agents', async (t) => {
  // Given
  const owned = { id: agentId, name: 'Owned agent', is_system: false, status: 'stopped', is_expired: false }
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ ...sourceMember, agents: [owned, { ...owned, id: targetId, is_system: true }] }))
  // When
  const source = await fetchTransferSource(sourceId)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/users/${sourceId}`)
  assert.deepEqual(transferableAgents(source).map((agent) => agent.id), [agentId])
})

test('missing system-agent flags fail closed instead of allowing unsafe agent selection', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...sourceMember, agents: [{ id: agentId, name: 'Unknown', status: 'stopped', is_expired: false }] }))
  // When / Then
  await assert.rejects(fetchTransferSource(sourceId), z.ZodError)
})

test('transfer posts only owner IDs and selected agent IDs and returns deletion cancellations', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(response))
  // When
  const result = await transferMemberAgents(input)
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/agent-ownership-transfers')
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.method, 'POST')
  assert.deepEqual(JSON.parse(String(fetch.mock.calls[0]?.arguments[1]?.body)), input)
  assert.deepEqual(result, response)
})

for (const ids of [[], Array(101).fill(agentId), [agentId, agentId]]) {
  test('empty, oversized and duplicated transfer batches fail before any request', async (t) => {
    // Given
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(response))
    // When / Then
    await assert.rejects(transferMemberAgents({ ...input, agent_ids: ids }), z.ZodError)
    assert.equal(fetch.mock.callCount(), 0)
  })
}

test('a transfer permits exactly 100 unique agent IDs and rejects self-transfers', () => {
  // Given
  const ids = Array.from({ length: 100 }, (_, index) => `00000000-0000-4000-8000-${index.toString(16).padStart(12, '0')}`)
  // When / Then
  assert.equal(agentTransferSchema.safeParse({ ...input, agent_ids: ids }).success, true)
  assert.equal(agentTransferSchema.safeParse({ ...input, to_user_id: sourceId }).success, false)
})

test('successful transfers invalidate source, recipient, user counts, reviews, and notifications', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const keys = [agentTransferKeys.source(sourceId), agentTransferKeys.targets(scope), ['admin-user', sourceId], ['admin-user', targetId], ['admin-users', tenantId], ['admin-companies'], agentDeletionKeys.all, notificationKeys.unread]
  for (const key of keys) client.setQueryData(key, {})
  t.mock.method(globalThis, 'fetch', async () => Response.json(response))
  const mutation = new MutationObserver(client, agentTransferOptions(client))
  // When
  await mutation.mutate(input)
  // Then
  assert.ok(keys.every((key) => client.getQueryState(key)?.isInvalidated))
})

for (const code of ['target_not_eligible', 'target_quota_exceeded', 'agent_not_owned_by_source', 'transfer_workspace_busy', 'transfer_retention_conflict']) {
  test(`${code} remains a structured conflict and is never retried automatically`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    client.setQueryData(agentTransferKeys.targets(scope), [])
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { code } }, { status: 409 }))
    const mutation = new MutationObserver(client, agentTransferOptions(client))
    // When / Then
    await assert.rejects(mutation.mutate(input), (error) => error instanceof ApiError && error.status === 409 && JSON.stringify(error.detail) === JSON.stringify({ code }))
    assert.equal(fetch.mock.callCount(), 1)
    assert.equal(client.getQueryState(agentTransferKeys.targets(scope))?.isInvalidated, true)
  })
}

test('retired transfer results cannot invalidate replacement-session user counts', async (t) => {
  // Given
  const { policy } = createFixture()
  const client = new QueryClient()
  t.after(() => { client.clear(); policy.logout() })
  client.setQueryData(['admin-users', tenantId], { total: 2 })
  const held = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.after(() => held.resolve(Response.json(response)))
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return held.promise })
  const mutation = new MutationObserver(client, ownMutationOptions(policy, policy.capture(), agentTransferOptions(client)))
  const result = assert.rejects(mutation.mutate(input), StaleSessionError)
  await started.promise
  // When
  policy.replace('new-owner')
  held.resolve(Response.json(response))
  await result
  // Then
  assert.equal(client.getQueryState(['admin-users', tenantId])?.isInvalidated, false)
})
