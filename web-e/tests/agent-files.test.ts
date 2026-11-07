import assert from 'node:assert/strict'
import { test } from 'node:test'
import { InfiniteQueryObserver, QueryClient } from '@tanstack/react-query'
import { ZodError } from 'zod'

import { createSessionQueryScope } from '../src/hooks/session-query-scope.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { agentFilesQuery, loadedFiles } from '../src/lib/agent-files-query.ts'
import { StaleSessionError } from '../src/lib/http.ts'
import { listFiles, listFilesPage, type FileInfo } from '../src/lib/workspace-api.ts'

const file = (name: string, is_dir = false): FileInfo => ({ name, path: name, is_dir, size: 0 })
const cursor = 'opaque/+?=&position'
const firstPage = { items: [file('a.txt'), file('docs', true)], next_cursor: cursor }

test('file pages encode the directory and opaque cursor and parse the native response', async (t) => {
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input), 'https://fixture.test')
    assert.equal(url.pathname, '/api/agents/agent%2Fone/files/page')
    assert.equal(url.searchParams.get('path'), 'workspace/a & b')
    assert.equal(url.searchParams.get('cursor'), cursor)
    assert.equal(url.searchParams.get('limit'), '12')
    assert.equal(init?.method, 'GET')
    return Response.json(firstPage)
  })
  const page = await listFilesPage('agent/one', 'workspace/a & b', { cursor, limit: 12 })
  assert.equal(page.next_cursor, cursor)
  assert.deepEqual(page.items.map((entry) => entry.path), ['a.txt', 'docs'])
})

for (const body of [[], { items: [{ ...file('bad'), is_dir: 'false' }], next_cursor: null }, { items: [], next_cursor: 12 }]) {
  test(`file pages reject malformed native response ${JSON.stringify(body)}`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json(body))
    await assert.rejects(listFilesPage('agent'), ZodError)
  })
}

test('unpaged listings keep the complete files endpoint', async (t) => {
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    const url = new URL(String(input), 'https://fixture.test')
    assert.equal(url.pathname, '/api/agents/agent/files/')
    assert.equal(url.searchParams.get('path'), 'skills')
    assert.equal(url.searchParams.has('limit'), false)
    return Response.json([file('one'), file('two')])
  })
  assert.deepEqual((await listFiles('agent', 'skills')).map((entry) => entry.name), ['one', 'two'])
})

test('loaded rows put folders first and retain file-folder collisions without mutating pages', () => {
  const original = file('same')
  const pages = [
    { items: [file('a.txt'), original, file('same', true)] },
    { items: [{ ...original, size: 99 }, file('z', true), file('z.txt')] },
  ]
  assert.deepEqual(loadedFiles(pages).map((entry) => [entry.path, entry.is_dir, entry.size]), [
    ['same', true, 0], ['z', true, 0], ['a.txt', false, 0], ['same', false, 0], ['z.txt', false, 0],
  ])
  assert.equal(pages[0]?.items[0]?.name, 'a.txt')
  assert.deepEqual(loadedFiles(undefined), [])
})

test('file traversal loads on demand, retains rows after a failure, retries the cursor, and isolates folders', async (t) => {
  const client = new QueryClient()
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  const requests: { path: string; cursor: string | null }[] = []
  let failed = false
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    const url = new URL(String(input), 'https://fixture.test')
    const path = url.searchParams.get('path') ?? ''
    const position = url.searchParams.get('cursor')
    requests.push({ path, cursor: position })
    if (position && !failed) { failed = true; return Response.json({ detail: 'Unavailable' }, { status: 503 }) }
    return Response.json(path ? { items: [file('inside')], next_cursor: null }
      : position ? { items: [file('z', true)], next_cursor: null } : firstPage)
  })
  const options = agentFilesQuery(scope, 'agent', '')
  const observer = new InfiniteQueryObserver(client, { ...options, staleTime: Infinity })
  const unsubscribe = observer.subscribe(() => {})
  t.after(() => { unsubscribe(); client.clear() })
  await observer.refetch()
  assert.deepEqual(requests, [{ path: '', cursor: null }])
  const failure = await observer.fetchNextPage()
  assert.equal(failure.isFetchNextPageError, true)
  assert.deepEqual(loadedFiles(failure.data?.pages).map((entry) => entry.name), ['docs', 'a.txt'])
  const retry = await observer.fetchNextPage()
  assert.equal(retry.hasNextPage, false)
  assert.deepEqual(retry.data?.pageParams, [null, cursor])
  observer.setOptions({ ...agentFilesQuery(scope, 'agent', 'docs'), staleTime: Infinity })
  const folder = await observer.refetch()
  assert.deepEqual(folder.data?.pageParams, [null])
  assert.deepEqual(loadedFiles(folder.data?.pages).map((entry) => entry.name), ['inside'])
  assert.deepEqual(requests, [{ path: '', cursor: null }, { path: '', cursor }, { path: '', cursor }, { path: 'docs', cursor: null }])
  assert.ok(client.getQueryCache().findAll({ queryKey: ['files', 'agent'] }).length > 0)
})

for (const retire of ['owner', 'signal'] as const) {
  test(`file pages discard a late response after ${retire} retirement`, async (t) => {
    const pending = Promise.withResolvers<Response>()
    const controller = new AbortController()
    t.mock.method(globalThis, 'fetch', () => pending.promise)
    const request = listFilesPage('agent', '', { signal: controller.signal })
    if (retire === 'owner') authSession.adoptStoredSession(authSession.capture())
    else controller.abort()
    pending.resolve(Response.json(firstPage))
    await assert.rejects(request, (error: unknown) => retire === 'owner'
      ? error instanceof StaleSessionError : error instanceof DOMException && error.name === 'AbortError')
  })
}
