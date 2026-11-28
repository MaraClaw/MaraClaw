import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { getSession, listSessionsPage, SESSION_PAGE_SIZE } from '../src/lib/chat-sessions-api.ts'
import { chatSessionKey, chatSessionsKey, controlSessionsKey, distinctSessions } from '../src/lib/chat-sessions-query.ts'
import { controlSessionId, isOwnConversation, isWritableSession } from '../src/lib/chat/sessions.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { assertForwardsCancellation } from './cancellation-fixture.ts'

const me = 'viewer'
const own = { id: 'own', source_channel: 'web', is_primary: true, user_id: me, writable: true }
const colleague = { id: 'colleague', source_channel: 'web', is_primary: true, user_id: 'colleague', writable: false }
const reply = { id: 'reply', source_channel: 'web', is_primary: false, user_id: me, writable: false }
const peer = { id: 'peer', source_channel: 'agent', user_id: me, writable: true }
const group = { id: 'group', source_channel: 'feishu', is_group: true, user_id: 'colleague', writable: true }

test('Given rows from the engine, only those it marks writable accept new turns, whatever their channel', () => {
  // Given a missing row, rows the engine refused (including a group) and an unflagged peer, then the rows it allows
  const sessions = [undefined, colleague, reply, { ...group, writable: false }, { id: 'unflagged', source_channel: 'agent' }, own, peer, group]
  // When
  const writable = sessions.map(isWritableSession)
  // Then
  assert.deepEqual(writable, [false, false, false, false, false, true, true, true])
})

test('Given rows the viewer can see, only their writable canonical row is their own conversation', () => {
  // Given / When
  const owned = [own, reply, peer, colleague, { ...colleague, writable: true }, { ...own, writable: false }, { ...own, is_group: true }]
    .map((session) => isOwnConversation(session, me))
  // Then
  assert.deepEqual(owned, [true, false, false, false, false, false, false])
  assert.equal(isOwnConversation(own, undefined), false)
})

test('Given no usable request, direct control falls back to your own conversation regardless of order', () => {
  // Given
  const sessions = [colleague, reply, peer, own]
  // When / Then
  assert.equal(controlSessionId(sessions, '', me), own.id)
  assert.equal(controlSessionId(sessions, colleague.id, me), own.id)
  assert.equal(controlSessionId(sessions, reply.id, me), own.id)
})

test('Given no own conversation, direct control never falls back to a colleague, a reply row or a peer session', () => {
  // Given / When / Then
  assert.equal(controlSessionId([colleague, { ...colleague, id: 'writable-colleague', writable: true }, reply, peer], '', me), undefined)
  assert.equal(controlSessionId([own], '', undefined), undefined)
})

for (const session of [peer, group]) {
  test(`Given an explicit writable ${session.id} session, direct control keeps its own scope`, () => {
    // Given / When
    const selected = controlSessionId([own, colleague, session], session.id, me)
    // Then
    assert.equal(selected, session.id)
  })
}

const agentId = '5458ec51-a954-4306-8626-68e16921d95f'
const row = (index: number, overrides: Record<string, unknown> = {}) => ({
  id: `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`, agent_id: agentId, user_id: 'viewer', title: `Thread ${index}`,
  source_channel: 'web', created_at: '2030-01-01T00:00:00Z', last_message_at: null, message_count: 1, unread_count: 0,
  is_primary: false, ...overrides,
})
const ownRow = row(0, { is_primary: true, writable: true })
const reservedCursor = 'opaque+/=? page&scope=all'

for (const cursor of [undefined, null, reservedCursor]) {
  test(`session pages request a bodyless scoped GET ${cursor ? 'with an encoded cursor' : 'for the first page'}`, async (context) => {
    // Given
    const caller = new AbortController()
    const { signal } = caller
    const forwarded: (AbortSignal | null | undefined)[] = []
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      const url = new URL(path, 'https://fixture.test')
      assert.equal(url.pathname, `/api/agents/${agentId}/sessions/page`)
      assert.deepEqual([...url.searchParams], [['scope', 'all'], ['limit', String(SESSION_PAGE_SIZE)], ...(cursor ? [['cursor', reservedCursor]] : [])])
      if (cursor) assert.match(path, /cursor=opaque%2B%2F%3D%3F\+page%26scope%3Dall$/)
      assert.equal(init.method, 'GET')
      assert.equal(init.body, undefined)
      forwarded.push(init.signal)
      return Response.json({ items: [ownRow], next_cursor: null, has_more: false })
    })
    // When
    const page = await listSessionsPage(agentId, 'all', { cursor, signal })
    // Then
    assert.deepEqual(page, { items: [ownRow], next_cursor: null, has_more: false })
    assertForwardsCancellation(forwarded, caller)
  })
}

test('session pages and lookups are parsed with the generated schema, not trusted as arrays', async (context) => {
  // Given
  const bodies = [
    [ownRow], {}, { items: [], has_more: false }, { items: [{ ...ownRow, id: undefined }], next_cursor: null, has_more: false },
    { items: [], next_cursor: 7, has_more: true }, { items: [], next_cursor: null, has_more: 'no' },
  ]
  for (const body of bodies) {
    context.mock.method(globalThis, 'fetch', async () => Response.json(body))
    // When / Then
    await assert.rejects(listSessionsPage(agentId, 'mine'), ZodError)
  }
  context.mock.method(globalThis, 'fetch', async () => Response.json({ ...ownRow, created_at: undefined }))
  await assert.rejects(getSession(agentId, ownRow.id), ZodError)
})

test('a session lookup requests the encoded session path and returns the validated row', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(new URL(path, 'https://fixture.test').pathname, `/api/agents/${agentId}/sessions/${encodeURIComponent('a/b c')}`)
    assert.equal(init.method, 'GET')
    return Response.json(row(5, { id: 'a/b c' }))
  })
  // When
  const found = await getSession(agentId, 'a/b c')
  // Then
  assert.equal(found.id, 'a/b c')
})

for (const status of [400, 403, 404, 422, 503]) {
  test(`session pages preserve HTTP ${status} as a request failure`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'nope' }, { status }))
    // When
    const result = listSessionsPage(agentId, 'all', { cursor: 'stale' })
    // Then
    await assert.rejects(result, (error) => error instanceof ApiError && error.status === status)
  })
}

test('a session page completing after the owner changes is discarded', async (context) => {
  // Given
  const owner = authSession.capture()
  const response = Response.json({ items: [], next_cursor: null, has_more: false })
  context.mock.method(response, 'text', async () => {
    authSession.logout(owner)
    return JSON.stringify({ items: [], next_cursor: null, has_more: false })
  })
  context.mock.method(globalThis, 'fetch', async () => response)
  // When / Then
  await assert.rejects(listSessionsPage(agentId, 'mine', { owner }), StaleSessionError)
})

test('loaded pages flatten without repeating a row a later page moved or re-sent', () => {
  // Given a session that moved up while the user scrolled and so appears on both pages.
  const first = { items: [ownRow, row(1), row(2)] }
  const second = { items: [row(2, { title: 'stale copy' }), row(3)] }
  // When
  const rows = distinctSessions([first, second])
  // Then the first copy wins, order is preserved and nothing repeats.
  assert.deepEqual(rows.map((session) => session.id), [ownRow, row(1), row(2), row(3)].map((session) => session.id))
  assert.equal(rows[2]?.title, 'Thread 2')
  assert.deepEqual(distinctSessions(undefined), [])
})

test('chat and Take Control keep separate caches per agent and scope', async (context) => {
  // Given a chat list for an org admin, a chat list for a member and a Take Control list on one agent.
  const client = new QueryClient()
  context.after(() => client.clear())
  const keys = [chatSessionsKey(agentId, 'all'), chatSessionsKey(agentId, 'mine'), controlSessionsKey(agentId), chatSessionKey(agentId, 'x')]
  // Then every key is distinct and none is a prefix-collision with another page's exact key.
  assert.equal(new Set(keys.map((key) => JSON.stringify(key))).size, keys.length)
  assert.deepEqual(chatSessionsKey(agentId, 'all'), ['sessions', agentId, 'all'])
  assert.notDeepEqual(controlSessionsKey(agentId), chatSessionsKey(agentId, 'mine'))
  // And data written for one key is invisible to the others.
  client.setQueryData(chatSessionsKey(agentId, 'mine'), { pages: [{ items: [row(9)] }], pageParams: [null] })
  assert.equal(client.getQueryData(controlSessionsKey(agentId)), undefined)
  assert.equal(client.getQueryData(chatSessionsKey(agentId, 'all')), undefined)
  // Invalidating the agent prefix still reaches every list and lookup.
  for (const key of keys) client.setQueryData(key, { stale: false })
  await client.invalidateQueries({ queryKey: ['sessions', agentId] })
  assert.ok(keys.every((key) => client.getQueryState(key)?.isInvalidated))
})

test('paging an infinite chat query walks opaque cursors once and Take Control fetches its own first page', async (context) => {
  // Given an engine with three pages for chat and a separate first-page request for Take Control.
  const client = new QueryClient()
  context.after(() => client.clear())
  const requests: { scope: string | null; cursor: string | null }[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    const params = new URL(path, 'https://fixture.test').searchParams
    requests.push({ scope: params.get('scope'), cursor: params.get('cursor') })
    const cursor = params.get('cursor')
    return Response.json(cursor === null ? { items: [ownRow, row(1)], next_cursor: reservedCursor, has_more: true }
      : cursor === reservedCursor ? { items: [row(2), row(1)], next_cursor: 'last', has_more: true }
        : { items: [row(3)], next_cursor: null, has_more: false })
  })
  const options = {
    queryKey: chatSessionsKey(agentId, 'all'), initialPageParam: null as string | null,
    queryFn: ({ pageParam }: { pageParam: string | null }) => listSessionsPage(agentId, 'all', { cursor: pageParam }),
    getNextPageParam: (page: { next_cursor: string | null }) => page.next_cursor ?? undefined,
  }
  // When chat loads every page and Take Control then loads its own list.
  await client.fetchInfiniteQuery({ ...options, pages: 3 })
  await client.fetchQuery({ queryKey: controlSessionsKey(agentId), queryFn: () => listSessionsPage(agentId, 'mine') })
  // Then chat advanced only with returned cursors, the repeated row collapses and Take Control never reused chat's data.
  const cached = client.getQueryData<{ pages: Awaited<ReturnType<typeof listSessionsPage>>[] }>(chatSessionsKey(agentId, 'all'))
  assert.deepEqual(distinctSessions(cached?.pages).map((session) => session.id), [ownRow, row(1), row(2), row(3)].map((session) => session.id))
  assert.deepEqual(requests, [
    { scope: 'all', cursor: null }, { scope: 'all', cursor: reservedCursor }, { scope: 'all', cursor: 'last' }, { scope: 'mine', cursor: null },
  ])
})
