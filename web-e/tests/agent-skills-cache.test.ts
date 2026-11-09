import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'

import { installedAgentSkillsOptions } from '../src/hooks/agent-skills-options.ts'
import { createSessionQueryScope } from '../src/hooks/session-query-scope.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { DiscoveryBodyResponse, skillFile } from './discovery-fixture.ts'

test('fresh installed queries reuse their agent cache instead of repeating Files discovery', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const paths: string[] = []
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    const url = new URL(String(input), 'http://fixture.test')
    assert.equal(url.searchParams.get('path'), 'skills')
    paths.push(url.pathname)
    return Response.json([{ name: 'report.md', path: 'skills/report.md', is_dir: false, size: 0 }])
  })
  const first = await client.fetchQuery(installedAgentSkillsOptions('agent-a'))
  // When
  const reopened = await client.fetchQuery(installedAgentSkillsOptions('agent-a'))
  await client.fetchQuery(installedAgentSkillsOptions('agent-b'))
  // Then
  assert.deepEqual(reopened, first)
  assert.deepEqual(paths, ['/api/agents/agent-a/files/', '/api/agents/agent-b/files/'])
})

for (const refresh of ['invalidated', 'expired'] as const) {
  test(`${refresh} installed queries refresh Files without invalidating another agent`, async (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    const options = installedAgentSkillsOptions('agent-a')
    const other = installedAgentSkillsOptions('agent-b')
    const before = [{ name: 'report', path: 'skills/report.md' }]
    client.setQueryData(options.queryKey, before, { updatedAt: Date.now() - (refresh === 'expired' ? 61_000 : 0) })
    client.setQueryData(other.queryKey, before)
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([]))
    if (refresh === 'invalidated') await client.invalidateQueries({ queryKey: ['files', 'agent-a'] })
    // When
    const result = await client.fetchQuery(options)
    const retained = await client.fetchQuery(other)
    // Then
    assert.deepEqual(result, [])
    assert.deepEqual(retained, before)
    assert.equal(fetch.mock.callCount(), 1)
  })
}

test('replacement session owners cannot reuse the previous owner installed cache', async (t) => {
  // Given
  const client = new QueryClient()
  const scope = createSessionQueryScope(authSession, authSession.capture(), client)
  const unsubscribe = scope.subscribe()
  t.after(() => { unsubscribe(); client.clear() })
  const options = installedAgentSkillsOptions('agent-a')
  client.setQueryData(options.queryKey, [{ name: 'old-owner-only', path: 'skills/old-owner-only.md' }])
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json([]))
  // When
  authSession.adoptStoredSession(authSession.capture())
  const result = await client.fetchQuery(options)
  // Then
  assert.deepEqual(result, [])
  assert.equal(fetch.mock.callCount(), 1)
})

for (const interruption of ['cancel-query', 'replace-owner'] as const) {
  test(`${interruption} aborts installed discovery and isolates another live query`, async (t) => {
    const client = new QueryClient()
    const scope = createSessionQueryScope(authSession, authSession.capture(), client)
    const unsubscribe = scope.subscribe()
    const replacementClient = new QueryClient()
    const body = new DiscoveryBodyResponse()
    const other = Promise.withResolvers<Response>()
    const options = installedAgentSkillsOptions('agent-a')
    const signals = new Map<string, AbortSignal | null | undefined>()
    t.after(() => { body.result.resolve('[]'); other.resolve(Response.json([])); unsubscribe(); client.clear(); replacementClient.clear() })
    t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(String(input), 'http://fixture.test')
      signals.set(url.pathname, init?.signal)
      return url.pathname.includes('agent-a') ? body : other.promise
    })
    const rejected = assert.rejects(client.fetchQuery(options), { name: 'Error' })
    await body.started.promise
    const liveOther = interruption === 'cancel-query' ? client.fetchQuery(installedAgentSkillsOptions('agent-b')) : undefined
    if (interruption === 'cancel-query') await client.cancelQueries({ queryKey: options.queryKey })
    else authSession.adoptStoredSession(authSession.capture())
    const otherResult = liveOther ?? replacementClient.fetchQuery(installedAgentSkillsOptions('agent-b'))
    body.result.resolve(JSON.stringify([skillFile('skills/old-owner.md')]))
    other.resolve(Response.json([skillFile('skills/current.md')]))
    await rejected
    assert.deepEqual(await otherResult, [{ name: 'current', path: 'skills/current.md' }])
    assert.equal(signals.get('/api/agents/agent-a/files/')?.aborted, true)
    assert.equal(signals.get('/api/agents/agent-b/files/')?.aborted, false)
    assert.equal(client.getQueryData(options.queryKey), undefined)
  })
}
