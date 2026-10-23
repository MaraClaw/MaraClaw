import assert from 'node:assert/strict'
import { test } from 'node:test'
import { InfiniteQueryObserver, QueryClient } from '@tanstack/react-query'

import { createSessionQueryScope, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { canViewTriggerExecutions, triggerExecutionQuery } from '../src/lib/trigger-executions-query.ts'
import { execution, firstPage, target } from './trigger-executions-fixture.ts'

for (const [access, role, eligible] of [
  ['manage', 'member', true], ['use', 'org_admin', true], ['use', 'platform_admin', true],
  ['use', 'member', false], ['use', 'agent_admin', false], ['unknown', 'member', false],
] as const) {
  test(`history eligibility for ${role} with ${access} access is ${eligible}`, () => {
    // Given
    const agent = { access_level: access }
    // When
    const result = canViewTriggerExecutions(agent, { role })
    // Then
    assert.equal(result, eligible)
  })
}

test('history remains ineligible before the agent has loaded even for an admin', () => {
  // Given
  const user = { role: 'org_admin' }
  // When
  const result = canViewTriggerExecutions(undefined, user)
  // Then
  assert.equal(result, false)
})

test('history isolates agents and triggers and retains page identities through a failed additional page', async (context) => {
  // Given
  const client = new QueryClient()
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  const cursors: (string | null)[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    const cursor = new URL(path, 'https://fixture.test').searchParams.get('cursor')
    cursors.push(cursor)
    if (cursor && cursors.length === 2) return Response.json({}, { status: 503 })
    return Response.json(cursor ? { items: [{ ...execution, id: '54903fdd-0609-472d-a73c-6af08846e2b7' }], next_cursor: null } : firstPage)
  })
  const options = triggerExecutionQuery(scope, target, 'mounted')
  const observer = new InfiniteQueryObserver(client, { ...options, staleTime: Infinity })
  const unsubscribe = observer.subscribe(() => {})
  context.after(() => { unsubscribe(); client.clear() })
  await observer.refetch()
  // When
  const failure = await observer.fetchNextPage()
  // Then
  assert.equal(failure.isFetchNextPageError, true)
  assert.deepEqual(failure.data?.pages, [firstPage])
  const retry = await observer.fetchNextPage()
  assert.equal(retry.data?.pages.length, 2)
  assert.deepEqual(retry.data?.pageParams, [null, firstPage.next_cursor])
  assert.deepEqual(cursors, [null, firstPage.next_cursor, firstPage.next_cursor])
  assert.equal(retry.hasNextPage, false)
  assert.equal(client.getQueryData(triggerExecutionQuery(scope, { ...target, triggerId: 'another' }, 'mounted').queryKey), undefined)
  assert.equal(client.getQueryData(triggerExecutionQuery(scope, { ...target, agentId: 'another' }, 'mounted').queryKey), undefined)
})

for (const status of [200, 401, 503]) {
  test(`history retirement aborts and clears cached pages and discards late ${status}`, async (context) => {
    // Given
    const client = new QueryClient()
    const scope = createSessionQueryScope(authSession, authSession.capture(), client)
    const retire = scope.subscribe()
    const requested = Promise.withResolvers<AbortSignal | null | undefined>()
    const response = Promise.withResolvers<Response>()
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      if (!new URL(path, 'https://fixture.test').searchParams.has('cursor')) return Response.json(firstPage)
      requested.resolve(init.signal)
      return response.promise
    })
    const options = triggerExecutionQuery(scope, target, 'retiring')
    const observer = new InfiniteQueryObserver(client, { ...options, staleTime: Infinity })
    const unsubscribe = observer.subscribe(() => {})
    context.after(() => { unsubscribe(); retire(); client.clear() })
    await observer.refetch()
    const pending = observer.fetchNextPage()
    const signal = await requested.promise
    // When
    authSession.logout(scope.owner)
    response.resolve(Response.json(status === 200 ? { items: [execution], next_cursor: null } : {}, { status }))
    await pending
    // Then
    assert.equal(signal?.aborted, true)
    assert.equal(client.getQueryCache().getAll().length, 0)
    await assert.rejects(scope.run(() => client.fetchInfiniteQuery(options)), RetiredWorkspaceError)
  })
}
