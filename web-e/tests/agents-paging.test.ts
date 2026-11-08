import assert from 'node:assert/strict'
import { test } from 'node:test'
import { InfiniteQueryObserver, QueryClient } from '@tanstack/react-query'
import { ZodError } from 'zod'
import { listAgentsPage } from '../src/lib/agents-api.ts'
import { agentExistenceOptions, agentPageOptions, distinctAgents } from '../src/lib/agents-query.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { StaleSessionError } from '../src/lib/http.ts'

const agent = {
  id: '11111111-1111-4111-8111-111111111111', name: 'Assistant', role_description: '',
  status: 'stopped', creator_id: '11111111-1111-4111-8111-111111111111', autonomy_policy: {},
  tokens_used_today: 0, tokens_used_month: 0, created_at: null,
}
const cursor = 'opaque+/=? page&company=other'

test('agent pages encode opaque cursors, forward cancellation and parse nullable dates', async (context) => {
  const signal = new AbortController().signal
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    const url = new URL(path, 'https://fixture.test')
    assert.equal(url.pathname, '/api/agents/page')
    assert.deepEqual([...url.searchParams], [['limit', '50'], ['cursor', cursor]])
    assert.match(path, /opaque%2B%2F%3D%3F\+page%26company%3Dother/)
    assert.equal(init.signal, signal)
    assert.equal(init.method, 'GET')
    return Response.json({ items: [agent], next_cursor: null, has_more: false })
  })
  const page = await listAgentsPage({ cursor, signal })
  assert.equal(page.items[0]?.created_at, null)
})

test('agent pages reject malformed rows and inconsistent continuation metadata', async (context) => {
  for (const body of [
    [agent], { items: [{ ...agent, status: 'unknown' }], next_cursor: null, has_more: false },
    { items: [agent], next_cursor: cursor, has_more: false },
  ]) {
    context.mock.method(globalThis, 'fetch', async () => Response.json(body))
    await assert.rejects(listAgentsPage(), ZodError)
  }
})

test('manual continuation retains its page on failure, retries the cursor and deduplicates live rows', async (context) => {
  const client = new QueryClient()
  context.after(() => client.clear())
  const requests: (string | null)[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    const next = new URL(path, 'https://fixture.test').searchParams.get('cursor')
    requests.push(next)
    return next === null ? Response.json({ items: [agent], next_cursor: cursor, has_more: true })
      : requests.length === 2 ? Response.json({ detail: 'Unavailable' }, { status: 503 })
        : Response.json({ items: [agent, { ...agent, id: '22222222-2222-4222-8222-222222222222' }], next_cursor: null, has_more: false })
  })
  const observer = new InfiniteQueryObserver(client, agentPageOptions())
  const first = await observer.refetch()
  assert.deepEqual(requests, [null])
  const failed = await observer.fetchNextPage()
  assert.equal(failed.isFetchNextPageError, true)
  assert.deepEqual(failed.data, first.data)
  const retried = await observer.fetchNextPage()
  assert.equal(retried.hasNextPage, false)
  assert.deepEqual(distinctAgents(retried.data?.pages).map((row) => row.id), [agent.id, '22222222-2222-4222-8222-222222222222'])
  assert.deepEqual(requests, [null, cursor, cursor])
  observer.destroy()
})

test('existence probes one row once and caches independently of the infinite directory', async (context) => {
  const client = new QueryClient()
  context.after(() => client.clear())
  const requests: string[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    requests.push(path)
    return Response.json({ items: [agent], next_cursor: cursor, has_more: true })
  })
  await client.fetchQuery(agentExistenceOptions())
  assert.deepEqual(requests, ['/api/agents/page?limit=1'])
  assert.equal(client.getQueryData(agentPageOptions().queryKey), undefined)
  await client.fetchInfiniteQuery(agentPageOptions())
  assert.equal(requests.length, 2)
  assert.equal(requests[1], '/api/agents/page?limit=50')
})

test('an agent continuation completing after owner retirement is discarded', async (context) => {
  const owner = authSession.capture()
  const response = Response.json({ items: [agent], next_cursor: null, has_more: false })
  context.mock.method(response, 'text', async () => {
    authSession.logout(owner)
    return JSON.stringify({ items: [agent], next_cursor: null, has_more: false })
  })
  context.mock.method(globalThis, 'fetch', async () => response)
  await assert.rejects(listAgentsPage({ cursor, owner }), StaleSessionError)
})
