import assert from 'node:assert/strict'
import { test } from 'node:test'

import { listInstalledAgentSkills } from '../src/lib/agent-skills-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import type { FileInfo } from '../src/lib/workspace-api.ts'
import { DiscoveryBodyResponse } from './discovery-fixture.ts'

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

for (const status of [403, 404, 500]) test(`a child ${status} remains a whole-discovery error rather than a missing root or successful subset`, async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    const path = new URL(String(input), 'http://fixture.test').searchParams.get('path')
    return path === 'skills' ? Response.json([file('skills/removed', true)]) : Response.json({ detail: 'Unavailable' }, { status })
  })
  // When / Then
  await assert.rejects(listInstalledAgentSkills('agent-a'), (error: unknown) => error instanceof ApiError && error.status === status)
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

test('large discovery admits four folders at a time and retains input precedence despite completion order', async (t) => {
  const names = Array.from({ length: 25 }, (_, index) => `folder-${index}`)
  const admitted = names.map(() => Promise.withResolvers<void>())
  const pending: { readonly path: string; readonly body: DiscoveryBodyResponse }[] = []
  t.after(() => { for (const { body } of pending) body.result.resolve('[]') })
  let active = 0
  let peak = 0
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    const path = new URL(String(input), 'http://fixture.test').searchParams.get('path') ?? ''
    if (path === 'skills') return Response.json(names.map((name) => file(`skills/${name}`, true)))
    const body = new DiscoveryBodyResponse()
    active++
    peak = Math.max(peak, active)
    pending.push({ path, body })
    admitted[pending.length - 1]?.resolve()
    return body
  })
  const request = listInstalledAgentSkills('agent-a')
  for (let offset = 0; offset < names.length; offset += 4) {
    await admitted[Math.min(offset + 3, names.length - 1)]?.promise
    const batch = pending.slice(offset)
    assert.equal(batch.length, Math.min(4, names.length - offset))
    for (const { path, body } of batch.toReversed()) {
      await body.started.promise
      active--
      body.result.resolve(JSON.stringify([file(`${path}/SKILL.md`)]))
    }
  }
  assert.deepEqual(await request, names.map((name) => ({ name, path: `skills/${name}/SKILL.md` }))
    .sort((left, right) => left.name.localeCompare(right.name)))
  assert.equal(peak, 4)
})

for (const stage of ['root', 'child-fetch', 'child-body'] as const) {
  test(`cancellation at ${stage} aborts active transport and never returns a subset or admits queued folders`, async (t) => {
    const controller = new AbortController()
    const started = Promise.withResolvers<void>()
    const body = new DiscoveryBodyResponse()
    const signals: (AbortSignal | null | undefined)[] = []
    const paths: string[] = []
    t.after(() => { controller.abort(); body.result.resolve('[]') })
    t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
      const path = new URL(String(input), 'http://fixture.test').searchParams.get('path') ?? ''
      signals.push(init?.signal)
      paths.push(path)
      if (path === 'skills' && stage !== 'root') {
        return Response.json(Array.from({ length: 20 }, (_, index) => file(`skills/folder-${index}`, true))
          .concat(file('skills/flat.md')))
      }
      started.resolve()
      if (stage === 'child-fetch') {
        if (!init?.signal) return Response.json([])
        return new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true })
        })
      }
      return body
    })
    const rejected = assert.rejects(listInstalledAgentSkills('agent-a', controller.signal), { name: 'AbortError' })
    await started.promise
    if (stage !== 'child-fetch') await body.started.promise
    controller.abort()
    body.result.resolve('[]')
    await rejected
    await new Promise<void>((resolve) => setImmediate(resolve))
    assert.equal(paths.length, stage === 'root' ? 1 : 5)
    assert.ok(signals.every((signal) => signal?.aborted))
  })
}

test('owner replacement during a child body rejects discovery and stops pending folder admission', async (t) => {
  const body = new DiscoveryBodyResponse()
  const paths: string[] = []
  t.after(() => body.result.resolve('[]'))
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request) => {
    const path = new URL(String(input), 'http://fixture.test').searchParams.get('path') ?? ''
    paths.push(path)
    return path === 'skills'
      ? Response.json(Array.from({ length: 20 }, (_, index) => file(`skills/folder-${index}`, true))) : body
  })
  const rejected = assert.rejects(listInstalledAgentSkills('agent-a'), StaleSessionError)
  await body.started.promise
  authSession.adoptStoredSession(authSession.capture())
  body.result.resolve('[]')
  await rejected
  assert.equal(paths.length, 5)
})
