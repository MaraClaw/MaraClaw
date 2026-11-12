import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { InfiniteQueryObserver, QueryClient } from '@tanstack/react-query'
import react from '@vitejs/plugin-react'
import { createElement, type ComponentType, type Context, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { createServer } from 'vite'
import { ZodError } from 'zod'
import { createSessionQueryScope, type SessionQueryScope } from '../src/hooks/session-query-scope.ts'
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

test('cached agents remain visible with a retry and toast after a failed refetch, separate from continuation failures', async (context) => {
  const root = fileURLToPath(new URL('..', import.meta.url))
  const cacheDir = await mkdtemp(join(tmpdir(), 'agents-paging-'))
  const server = await createServer({
    configFile: false, root, cacheDir,
    plugins: [react(), {
      name: 'observe-request-error-presentation',
      enforce: 'pre',
      load(id) {
        // SSR does not run effects; expose the toast leaf's message to inspect the real page's error branch.
        if (id === join(root, 'src/components/request-error-toast.tsx')) {
          return 'export function RequestErrorToast({ message }) { return message }'
        }
      },
    }],
    resolve: { alias: { '@': join(root, 'src') } },
    server: { middlewareMode: true, watch: null, hmr: false },
    appType: 'custom',
  })
  context.after(async () => { await server.close(); await rm(cacheDir, { recursive: true, force: true }) })
  const AgentsListPage: ComponentType = (await server.ssrLoadModule('/src/pages/app/agents-list.tsx'))['AgentsListPage']
  const TranslationProvider: ComponentType<{ readonly children?: ReactNode }> = (await server.ssrLoadModule('/src/hooks/translation-provider.tsx'))['TranslationProvider']
  const WorkspaceSessionContext: Context<SessionQueryScope | null> = (await server.ssrLoadModule('/src/hooks/session-workspace.ts'))['WorkspaceSessionContext']
  const client = new QueryClient({ defaultOptions: { queries: { refetchOnMount: false } } })
  context.after(() => client.clear())
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  let response = () => Response.json({ items: [agent], next_cursor: null, has_more: false })
  context.mock.method(globalThis, 'fetch', async () => response())
  const observer = new InfiniteQueryObserver(client, agentPageOptions())
  context.after(() => observer.destroy())
  const render = () => renderToStaticMarkup(createElement(TranslationProvider, null, createElement(MemoryRouter, null,
    createElement(WorkspaceSessionContext, { value: scope }, createElement(AgentsListPage)))))

  const first = await observer.refetch()
  assert.equal(first.isSuccess, true)
  assert.match(render(), /Assistant/)
  response = () => Response.json({ detail: 'Unavailable' }, { status: 503 })
  await observer.refetch()
  const failed = observer.getCurrentResult()
  assert.equal(failed.isError, true)
  assert.equal(failed.isFetchNextPageError, false)
  assert.equal(failed.hasNextPage, false)
  assert.deepEqual(failed.data, first.data)
  const refreshFailure = render()
  assert.match(refreshFailure, /Assistant/)
  assert.match(refreshFailure, /Could not load agents\. Try again\./)
  assert.match(refreshFailure, /Retry loading agents/)
  assert.doesNotMatch(refreshFailure, /Could not load more agents/)

  response = () => Response.json({ items: [agent], next_cursor: cursor, has_more: true })
  await observer.refetch()
  response = () => Response.json({ detail: 'Unavailable' }, { status: 503 })
  const continuation = await observer.fetchNextPage()
  assert.equal(continuation.isFetchNextPageError, true)
  const continuationFailure = render()
  assert.match(continuationFailure, /Assistant/)
  assert.match(continuationFailure, /Could not load more agents\. Your loaded agents are still available\./)
  assert.match(continuationFailure, /Retry loading more/)
  assert.doesNotMatch(continuationFailure, /Could not load agents\. Try again\.|Retry loading agents/)
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
