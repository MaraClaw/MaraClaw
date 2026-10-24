import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient, QueryObserver } from '@tanstack/react-query'

import { createSessionQueryScope, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { chatHistoryKey, chatLatestOptions, loadEarlierHistory } from '../src/lib/chat-history-query.ts'
import type { ChatHistory } from '../src/lib/chat/history.ts'
import { historyCursor, historyPage, historyRows, historyTarget } from './chat-history-fixture.ts'

test('newest refresh bridges more than a page of arrivals without refetching loaded older pages', async (context) => {
  // Given
  const client = new QueryClient()
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  client.setQueryData(chatHistoryKey(historyTarget), historyPage(0, 100, historyCursor))
  const requests: (string | null)[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    const cursor = new URL(path, 'https://fixture.test').searchParams.get('cursor')
    requests.push(cursor)
    return Response.json(cursor === null ? historyPage(200, 250, 'bridge-200')
      : cursor === 'bridge-200' ? historyPage(150, 200, 'bridge-150')
        : cursor === 'bridge-150' ? historyPage(100, 150, 'bridge-100') : historyPage(50, 100, 'unused-50'))
  })
  // When
  await client.fetchQuery(chatLatestOptions(scope, historyTarget))
  // Then
  assert.deepEqual(client.getQueryData<ChatHistory>(chatHistoryKey(historyTarget))?.items, historyRows(0, 250))
  assert.equal(client.getQueryData<ChatHistory>(chatHistoryKey(historyTarget))?.next_cursor, historyCursor)
  assert.deepEqual(requests, [null, 'bridge-200', 'bridge-150', 'bridge-100'])
})

test('a failed gap bridge leaves the complete previous range and cursor untouched', async (context) => {
  // Given
  const client = new QueryClient()
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  const retained = historyPage(0, 50, historyCursor)
  client.setQueryData(chatHistoryKey(historyTarget), retained)
  context.mock.method(globalThis, 'fetch', async (path: string) => new URL(path, 'https://fixture.test').searchParams.has('cursor')
    ? Response.json({}, { status: 503 }) : Response.json(historyPage(100, 150, 'gap')))
  // When / Then
  await assert.rejects(client.fetchQuery(chatLatestOptions(scope, historyTarget)))
  assert.deepEqual(client.getQueryData(chatHistoryKey(historyTarget)), retained)
})

test('earlier errors are retryable and cannot poison newest readiness or refetch on latest invalidation', async (context) => {
  // Given
  const client = new QueryClient()
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  let olderRequests = 0
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    if (!new URL(path, 'https://fixture.test').searchParams.has('cursor')) return Response.json(historyPage(50, 100, historyCursor))
    olderRequests++
    return olderRequests === 1 ? Response.json({}, { status: 503 }) : Response.json(historyPage(0, 51))
  })
  const observer = new QueryObserver(client, chatLatestOptions(scope, historyTarget))
  const unsubscribe = observer.subscribe(() => {})
  context.after(() => { unsubscribe(); client.clear() })
  await observer.refetch()
  await assert.rejects(loadEarlierHistory(scope, historyTarget, new AbortController().signal))
  await loadEarlierHistory(scope, historyTarget, new AbortController().signal)
  // When
  await client.invalidateQueries({ queryKey: ['messages', historyTarget.agentId, historyTarget.sessionId] })
  // Then
  assert.equal(observer.getCurrentResult().isSuccess, true)
  assert.deepEqual(client.getQueryData<ChatHistory>(chatHistoryKey(historyTarget))?.items, historyRows(0, 100))
  assert.equal(client.getQueryData<ChatHistory>(chatHistoryKey(historyTarget))?.next_cursor, null)
  assert.equal(olderRequests, 2)
})

test('concurrent older loading and newest refresh retain both commits and the older cursor chain', async (context) => {
  // Given
  const client = new QueryClient()
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  client.setQueryData(chatHistoryKey(historyTarget), historyPage(50, 100, historyCursor))
  const requested = Promise.withResolvers<void>()
  const response = Promise.withResolvers<Response>()
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    if (!new URL(path, 'https://fixture.test').searchParams.has('cursor')) return Response.json(historyPage(75, 125, 'refresh-cursor'))
    requested.resolve()
    return response.promise
  })
  const earlier = loadEarlierHistory(scope, historyTarget, new AbortController().signal)
  await requested.promise
  await client.fetchQuery(chatLatestOptions(scope, historyTarget))
  // When
  response.resolve(Response.json(historyPage(0, 51)))
  await earlier
  // Then
  assert.deepEqual(client.getQueryData<ChatHistory>(chatHistoryKey(historyTarget))?.items, historyRows(0, 125))
  assert.equal(client.getQueryData<ChatHistory>(chatHistoryKey(historyTarget))?.next_cursor, null)
})

test('a retired session view aborts its page without changing retained history or another session cache', async (context) => {
  // Given
  const client = new QueryClient()
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  const retained = historyPage(50, 100, historyCursor)
  client.setQueryData(chatHistoryKey(historyTarget), retained)
  const other = { ...historyTarget, sessionId: 'other-session' }
  client.setQueryData(chatHistoryKey(other), historyPage(200, 210))
  const controller = new AbortController()
  const requested = Promise.withResolvers<void>()
  const response = Promise.withResolvers<Response>()
  context.mock.method(globalThis, 'fetch', async () => { requested.resolve(); return response.promise })
  const pending = loadEarlierHistory(scope, historyTarget, controller.signal)
  const rejection = assert.rejects(pending, { name: 'AbortError' })
  await requested.promise
  // When
  controller.abort()
  response.resolve(Response.json(historyPage(0, 50)))
  await rejection
  // Then
  assert.deepEqual(client.getQueryData(chatHistoryKey(historyTarget)), retained)
  assert.deepEqual(client.getQueryData(chatHistoryKey(other)), historyPage(200, 210))
})

for (const status of [200, 401, 503]) {
  test(`owner retirement cancels cached history work and discards late ${status}`, async (context) => {
    // Given
    const client = new QueryClient()
    const scope = createSessionQueryScope(authSession, authSession.capture(), client)
    const retire = scope.subscribe()
    context.after(() => { retire(); client.clear() })
    client.setQueryData(chatHistoryKey(historyTarget), historyPage(50, 100, historyCursor))
    const requested = Promise.withResolvers<AbortSignal | null | undefined>()
    const response = Promise.withResolvers<Response>()
    context.mock.method(globalThis, 'fetch', async (_: string, init: RequestInit) => { requested.resolve(init.signal); return response.promise })
    const pending = loadEarlierHistory(scope, historyTarget, new AbortController().signal)
    const rejection = assert.rejects(pending)
    const signal = await requested.promise
    // When
    authSession.logout(scope.owner)
    response.resolve(Response.json(status === 200 ? historyPage(0, 50) : {}, { status }))
    await rejection
    // Then
    assert.equal(signal?.aborted, true)
    assert.equal(client.getQueryCache().getAll().length, 0)
    await assert.rejects(scope.run(() => client.fetchQuery(chatLatestOptions(scope, historyTarget))), RetiredWorkspaceError)
  })
}
