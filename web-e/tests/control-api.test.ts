import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { createCredential, listCredentials, listPublishedPages, updateCredential, controlUnlock } from '../src/lib/control-api.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { listFiles } from '../src/lib/workspace-api.ts'
import { DiscoveryBodyResponse } from './discovery-fixture.ts'

const credential = {
  id: '09bc8102-a06b-4cb5-b034-3e32ea78f67a', agent_id: '5458ec51-a954-4306-8626-68e16921d95f', revision: 7,
  credential_type: 'website', platform: 'vendor.example.test', display_name: 'Team account', status: 'active',
  has_cookies: true, cookies_updated_at: null, last_login_at: null, last_injected_at: null,
  created_at: null, updated_at: null,
} as const
const target = { agentId: credential.agent_id, credentialId: credential.id }
const lease = { agentId: credential.agent_id, sessionId: 'control-session', ownerToken: 'private-owner-token' }

for (const operation of ['list', 'create', 'edit'] as const) {
  test(`${operation} returns only safe revision-bearing credential metadata`, async (context) => {
    // Given
    const raw = { ...credential, cookies_json: 'secret-cookie', encrypted: 'secret-ciphertext', internal: true }
    context.mock.method(globalThis, 'fetch', async () => Response.json(operation === 'list' ? [raw] : raw))
    // When
    const result = operation === 'list' ? await listCredentials(target.agentId)
      : operation === 'create' ? await createCredential(target.agentId, { platform: credential.platform })
        : await updateCredential(target, { expected_revision: 6, display_name: credential.display_name })
    // Then
    assert.deepEqual(result, operation === 'list' ? [credential] : credential)
  })
}

for (const revision of [undefined, null, 0, -1, 1.5, '7', Number.MAX_SAFE_INTEGER + 1]) {
  test(`vault rejects unusable revision ${String(revision)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json([{ ...credential, revision }]))
    // When
    const result = listCredentials(target.agentId)
    // Then
    await assert.rejects(result, ZodError)
  })
}

test('credential edits send the observed revision rather than the returned revision', async (context) => {
  // Given
  let request: unknown
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, `/api/agents/${target.agentId}/credentials/${target.credentialId}`)
    assert.equal(init.method, 'PUT')
    request = JSON.parse(String(init.body))
    return Response.json({ ...credential, revision: 8 })
  })
  // When
  const result = await updateCredential(target, { expected_revision: 7, display_name: 'Renamed' })
  // Then
  assert.deepEqual(request, { expected_revision: 7, display_name: 'Renamed' })
  assert.equal(result.revision, 8)
})

test('credential edit rejects invalid preconditions before issuing a request', async (context) => {
  // Given
  const fetch = context.mock.method(globalThis, 'fetch')
  // When
  const result = updateCredential(target, { expected_revision: 0, display_name: 'Renamed' })
  // Then
  await assert.rejects(result, ZodError)
  assert.equal(fetch.mock.callCount(), 0)
})

for (const capture of [{ mode: 'create' }, { mode: 'replace', credential_id: credential.id, expected_revision: 7 }] as const) {
  test(`unlock forwards explicit ${capture.mode} intent with the lease`, async (context) => {
    // Given
    let body: unknown
    context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
      body = JSON.parse(String(init.body))
      return Response.json({ status: 'unlocked', cookies_exported: true, cookie_count: 2 })
    })
    // When
    await controlUnlock(lease, { export_cookies: true, platform_hint: credential.platform, capture })
    // Then
    assert.deepEqual(body, {
      session_id: lease.sessionId, owner_token: lease.ownerToken, export_cookies: true, platform_hint: credential.platform, capture,
    })
  })
}

test('unlock without capture choice never exports cookies implicitly', async (context) => {
  // Given
  let body: unknown
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    body = JSON.parse(String(init.body))
    return Response.json({ status: 'unlocked' })
  })
  // When
  await controlUnlock(lease)
  // Then
  assert.deepEqual(body, { session_id: lease.sessionId, owner_token: lease.ownerToken, export_cookies: false })
})

for (const code of ['credential_revision_conflict', 'take_control_lost']) {
  test(`unlock preserves distinct ${code} failure`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { code } }, { status: 409 }))
    // When
    const result = controlUnlock(lease, { export_cookies: true, platform_hint: credential.platform, capture: { mode: 'create' } })
    // Then
    await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === 409 &&
      typeof error.detail === 'object' && error.detail !== null && 'code' in error.detail && error.detail.code === code)
  })
}

for (const operation of ['list', 'edit', 'unlock'] as const) {
  test(`${operation} discards responses after its session owner retires`, async (context) => {
    // Given
    const owner = authSession.capture()
    context.mock.method(globalThis, 'fetch', async () => {
      authSession.logout(owner)
      return Response.json(operation === 'list' ? [credential] : operation === 'edit' ? credential : { status: 'unlocked' })
    })
    // When
    const result = operation === 'list' ? listCredentials(target.agentId, { owner })
      : operation === 'edit' ? updateCredential(target, { expected_revision: 7 }, { owner }) : controlUnlock(lease)
    // Then
    await assert.rejects(result, StaleSessionError)
  })
}

for (const resource of ['files', 'pages'] as const) {
  const list = (signal?: AbortSignal) => resource === 'files'
    ? listFiles('agent-a', 'workspace/a & b', signal) : listPublishedPages('agent-a', signal)

  test(`${resource} preserves its discovery URL and returns results with or without a signal`, async (t) => {
    const controller = new AbortController()
    const requests: { readonly url: string; readonly signal: AbortSignal | null | undefined }[] = []
    t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
      requests.push({ url: String(input), signal: init?.signal })
      return Response.json([])
    })
    assert.deepEqual(await list(), [])
    assert.deepEqual(await list(controller.signal), [])
    const url = resource === 'files' ? '/api/agents/agent-a/files/?path=workspace%2Fa%20%26%20b' : '/api/pages/list?agent_id=agent-a'
    assert.deepEqual(requests, [{ url, signal: undefined }, { url, signal: controller.signal }])
  })

  for (const interruption of ['abort', 'replacement'] as const) {
    test(`${resource} rejects ${interruption} while its response body is pending`, async (t) => {
      const controller = new AbortController()
      const body = new DiscoveryBodyResponse()
      t.mock.method(globalThis, 'fetch', async () => body)
      const rejected = interruption === 'abort'
        ? assert.rejects(list(controller.signal), { name: 'AbortError' }) : assert.rejects(list(), StaleSessionError)
      await body.started.promise
      if (interruption === 'abort') controller.abort()
      else authSession.adoptStoredSession(authSession.capture())
      body.result.resolve('[]')
      await rejected
    })
  }
}
