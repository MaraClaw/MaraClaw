import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver, QueryClient } from '@tanstack/react-query'

import { createSessionQueryScope, fenceMutationOptions } from '../src/hooks/session-query-scope.ts'
import { getAgentDeletionRequest, listPendingAgentDeletions, requestAgentDeletion } from '../src/lib/agent-deletion-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { StaleSessionError } from '../src/lib/http.ts'
import { agentId, deletionDetail, deletionSession, deletionSummary, requestId } from './agent-deletion-fixture.ts'

for (const retirement of ['logout', 'tenant-switch'] as const) {
  for (const status of [201, 409, 401, 503]) {
    test(`late deletion ${status} after ${retirement} cannot run workspace completion effects`, async (t) => {
      // Given
      const { owner, user } = deletionSession(t)
      const client = new QueryClient({ defaultOptions: { mutations: { retry: false, gcTime: Infinity } } })
      const scope = createSessionQueryScope(authSession, owner, client)
      const unsubscribe = scope.subscribe()
      t.after(() => { unsubscribe(); client.clear() })
      const requested = Promise.withResolvers<void>()
      const response = Promise.withResolvers<Response>()
      t.mock.method(globalThis, 'fetch', async () => { requested.resolve(); return response.promise })
      let effects = 0
      const observer = new MutationObserver(client, fenceMutationOptions(scope, {
        mutationFn: () => requestAgentDeletion({ agent_id: agentId, reason: 'Reason' }, { owner }),
        onSuccess: () => { effects++ }, onError: () => { effects++ }, onSettled: () => { effects++ },
      }))
      const pending = observer.mutate(undefined)
      const rejected = assert.rejects(pending, StaleSessionError)
      await requested.promise
      // When
      if (retirement === 'logout') authSession.logout(owner)
      else authSession.applySession(owner, { access_token: 'replacement', user: { ...user, tenant_id: 'tenant-two' } })
      response.resolve(Response.json(status === 201 ? deletionDetail : { detail: { code: 'deletion_request_pending', request_id: requestId } }, { status }))
      await rejected
      // Then
      assert.equal(effects, 0)
      assert.equal(authSession.getSnapshot().token, retirement === 'logout' ? null : 'replacement')
      assert.equal(client.getMutationCache().getAll().length, 0)
    })
  }
}

for (const read of ['collection', 'detail'] as const) {
  test(`late ${read} response is discarded after tenant replacement`, async (t) => {
    // Given
    const { owner, user } = deletionSession(t)
    t.mock.method(globalThis, 'fetch', async () => {
      authSession.applySession(owner, { access_token: 'replacement', user: { ...user, tenant_id: 'tenant-two' } })
      return Response.json(read === 'collection' ? { items: [deletionSummary], next_cursor: null } : deletionDetail)
    })
    // When
    const result = read === 'collection' ? listPendingAgentDeletions(agentId, { owner }) : getAgentDeletionRequest(requestId, { owner })
    // Then
    await assert.rejects(result, StaleSessionError)
  })
}

test('pending lookup discards a response after cancellation', async (t) => {
  // Given
  const { owner } = deletionSession(t)
  const controller = new AbortController()
  t.mock.method(globalThis, 'fetch', async () => {
    controller.abort()
    return Response.json({ items: [deletionSummary], next_cursor: null })
  })
  // When / Then
  await assert.rejects(listPendingAgentDeletions(agentId, { owner, signal: controller.signal }), { name: 'AbortError' })
})
