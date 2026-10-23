import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'

import { installedAgentSkillsOptions } from '../src/hooks/agent-skills-options.ts'
import { createSessionQueryScope } from '../src/hooks/session-query-scope.ts'
import { authSession } from '../src/lib/auth-session.ts'

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
