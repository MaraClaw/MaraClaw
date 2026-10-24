import assert from 'node:assert/strict'
import { test } from 'node:test'

import { listInstalledAgentSkills } from '../src/lib/agent-skills-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import type { FileInfo } from '../src/lib/workspace-api.ts'

const file = (path: string, is_dir = false): FileInfo => ({ path, name: path.split('/').at(-1) ?? '', is_dir, size: 0 })

test('installed discovery uses agent files and verifies folder entry points while excluding hidden or incomplete entries', async (t) => {
  // Given
  const files = new Map<string, readonly FileInfo[]>([
    ['skills', ['audit-review', 'lowercase', 'no-entry', '.private'].map((name) => file(`skills/${name}`, true))
      .concat([file('skills/report.md'), file('skills/.secret.md'), file('skills/.md'), file('skills/README.txt'), file('skills/audit-review.md')])],
    ['skills/audit-review', [file('skills/audit-review/skill.md'), file('skills/audit-review/SKILL.md')]],
    ['skills/lowercase', [file('skills/lowercase/skill.md')]],
    ['skills/no-entry', [file('skills/no-entry/README.md'), file('skills/no-entry/SKILL.md', true)]],
  ])
  const paths: string[] = []
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input), 'http://fixture.test')
    assert.equal(url.pathname, '/api/agents/agent-a/files/')
    assert.equal(init?.method, 'GET')
    const path = url.searchParams.get('path') ?? ''
    paths.push(path)
    assert.ok(files.has(path), `Unexpected Files request: ${path}`)
    return Response.json(files.get(path))
  })
  // When
  const skills = await listInstalledAgentSkills('agent-a')
  // Then
  assert.deepEqual(skills, [
    { name: 'audit-review', path: 'skills/audit-review/SKILL.md' },
    { name: 'lowercase', path: 'skills/lowercase/skill.md' },
    { name: 'report', path: 'skills/report.md' },
  ])
  assert.deepEqual(paths.sort(), ['skills', 'skills/audit-review', 'skills/lowercase', 'skills/no-entry'])
})

test('a missing skills directory is an empty installed list', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Missing directory' }, { status: 404 }))
  // When
  const skills = await listInstalledAgentSkills('agent-a')
  // Then
  assert.deepEqual(skills, [])
  assert.equal(fetch.mock.callCount(), 1)
})

for (const status of [403, 500]) {
  test(`installed discovery preserves ${status} errors instead of claiming there are no skills`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status }))
    // When / Then
    await assert.rejects(listInstalledAgentSkills('agent-a'), (error: unknown) => error instanceof ApiError && error.status === status)
  })
}

test('an entry folder disappearing remains a discovery error rather than being mistaken for a missing skills directory', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    const path = new URL(String(input), 'http://fixture.test').searchParams.get('path')
    return path === 'skills' ? Response.json([file('skills/removed', true)]) : Response.json({ detail: 'Removed' }, { status: 404 })
  })
  // When / Then
  await assert.rejects(listInstalledAgentSkills('agent-a'), (error: unknown) => error instanceof ApiError && error.status === 404)
})

test('a late installed response cannot start folder discovery after owner replacement', async (t) => {
  // Given
  const pending = Promise.withResolvers<Response>()
  const fetch = t.mock.method(globalThis, 'fetch', () => pending.promise)
  const request = listInstalledAgentSkills('agent-a')
  t.after(() => { authSession.adoptStoredSession(authSession.capture()) })
  // When
  authSession.adoptStoredSession(authSession.capture())
  pending.resolve(Response.json([file('skills/stale', true)]))
  // Then
  await assert.rejects(request, StaleSessionError)
  assert.equal(fetch.mock.callCount(), 1)
})

test('a cancelled query stops discovery before its folder requests', async (t) => {
  // Given
  const controller = new AbortController()
  const pending = Promise.withResolvers<Response>()
  const fetch = t.mock.method(globalThis, 'fetch', () => pending.promise)
  const request = listInstalledAgentSkills('agent-a', controller.signal)
  // When
  controller.abort()
  pending.resolve(Response.json([file('skills/stale', true)]))
  // Then
  await assert.rejects(request, (error: unknown) => error instanceof DOMException && error.name === 'AbortError')
  assert.equal(fetch.mock.callCount(), 1)
})
