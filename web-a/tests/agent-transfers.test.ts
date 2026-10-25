import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { agentDeletionKeys } from '../src/hooks/agent-deletion-options.ts'
import { agentTransferKeys, agentTransferOptions } from '../src/hooks/agent-transfer-options.ts'
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

test('recipient loading follows all cursors and excludes foreign, inactive, source, and admin targets', async (t) => {
  // Given
  const controller = new AbortController()
  let calls = 0
  const fetch = t.mock.method(globalThis, 'fetch', async () => {
    calls += 1
    return Response.json(calls === 1 ? { items: [sourceMember, { ...targetMember, tenant_id: 'foreign' }, { ...targetMember, is_active: false }, { ...targetMember, role: 'org_admin' }], next_cursor: 'next+/=' }
      : { items: [{ ...targetMember, role: 'agent_admin' }], next_cursor: null })
  })
  // When
  const result = await fetchAgentTransferTargets(scope, controller.signal)
  // Then
  assert.deepEqual(result.map((member) => member.id), [targetId])
  assert.deepEqual(fetch.mock.calls.map((call) => call.arguments[0]), [`/api/users/page?tenant_id=${tenantId}&search=&limit=50`, `/api/users/page?tenant_id=${tenantId}&search=&limit=50&cursor=next%2B%2F%3D`])
  assert.ok(fetch.mock.calls.every((call) => call.arguments[1]?.signal === controller.signal))
})

test('aborting recipient pagination stops before fetching another page', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async () => { controller.abort(); return Response.json({ items: [], next_cursor: 'later' }) })
  // When / Then
  await assert.rejects(fetchAgentTransferTargets(scope, controller.signal), (error) => error instanceof Error && error.name === 'AbortError')
  assert.equal(fetch.mock.callCount(), 1)
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
