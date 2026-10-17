import assert from 'node:assert/strict'
import { once } from 'node:events'
import { createServer } from 'node:http'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { test } from 'node:test'
import type { TestContext } from 'node:test'
import { fetchCurrentUser, issueChatTicket, joinDefaultOrg, loginRequest, switchTenant } from '../src/lib/auth-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { createAuthSessionController } from '../src/lib/auth-session-controller.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import type { TokenResponse, UserOut } from '../src/lib/types/auth.ts'

const user: UserOut = {
  id: 'member', username: null, email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}
const session: TokenResponse = { access_token: 'token-a', user }

async function wire(t: TestContext, handle: (request: IncomingMessage, response: ServerResponse) => void) {
  const values = new Map<string, string>()
  const localStorage: Storage = {
    get length() { return values.size },
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, value) },
    removeItem: (key) => { values.delete(key) },
    clear: () => values.clear(),
    key: (index) => [...values.keys()][index] ?? null,
  }
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage } })
  const server = createServer(handle)
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('Expected ephemeral TCP address')
  const base = `http://127.0.0.1:${address.port}`
  const realFetch = globalThis.fetch
  t.mock.method(globalThis, 'fetch', (input: RequestInfo | URL, init?: RequestInit) =>
    realFetch(new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, base), init))
  t.after(async () => {
    authSession.logout(authSession.capture())
    if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow)
    else Reflect.deleteProperty(globalThis, 'window')
    server.closeAllConnections()
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  })
  authSession.applySession(authSession.capture(), session)
  return { localStorage, controller: createAuthSessionController(authSession, (token, owner, signal) => fetchCurrentUser(token, signal, owner)) }
}

function send(response: ServerResponse, status: number, body: unknown): void {
  response.writeHead(status, { 'content-type': 'application/json' })
  response.end(JSON.stringify(body))
}

for (const sessionId of [undefined, 'requested-session']) {
  test(`ticket issuer sends bearer HTTP and parses the canonical session (${sessionId ?? 'omitted'})`, async (t) => {
    const arrived = Promise.withResolvers<{ readonly path: string | undefined; readonly method: string | undefined; readonly authorization: string | undefined; readonly body: string }>()
    await wire(t, (request, response) => {
      let body = ''
      request.on('data', (chunk: Buffer) => { body += chunk.toString() })
      request.on('end', () => {
        arrived.resolve({ path: request.url, method: request.method, authorization: request.headers.authorization, body })
        send(response, 200, { ticket: 'opaque-wire-ticket', session_id: 'canonical-session' })
      })
    })
    const controller = new AbortController()
    const ticket = await issueChatTicket({ agent_id: 'agent-a', ...(sessionId === undefined ? {} : { session_id: sessionId }) }, authSession.capture(), controller.signal)
    const request = await arrived.promise
    assert.equal(request.path, '/api/auth/chat-ticket')
    assert.equal(request.method, 'POST')
    assert.equal(request.authorization, 'Bearer token-a')
    assert.deepEqual(JSON.parse(request.body), sessionId === undefined ? { agent_id: 'agent-a' } : { agent_id: 'agent-a', session_id: sessionId })
    assert.deepEqual(ticket, { ticket: 'opaque-wire-ticket', session_id: 'canonical-session' })
  })
}

for (const status of [401, 403]) {
  test(`real ticket HTTP ${status} ${status === 401 ? 'expires' : 'preserves'} the captured owner`, async (t) => {
    await wire(t, (_request, response) => send(response, status, { detail: 'denied' }))
    const owner = authSession.capture()
    await assert.rejects(issueChatTicket({ agent_id: 'agent-a' }, owner, new AbortController().signal),
      (error: unknown) => error instanceof ApiError && error.status === status)
    assert.equal(authSession.isCurrent(owner), status === 403)
    assert.equal(authSession.getSnapshot().token, status === 401 ? null : 'token-a')
  })
}

test('deferred login cannot apply or trigger effects after a same-token replacement', async (t) => {
  // Given
  const arrived = Promise.withResolvers<ServerResponse>()
  await wire(t, (_request, response) => { arrived.resolve(response) })
  const owner = authSession.capture()
  let effects = 0
  const login = loginRequest({ login_identifier: 'member@example.com', password: 'password' }, undefined, owner)
    .then((result) => {
      if ('user' in result && authSession.applySession(owner, result)) effects++
    })
  const response = await arrived.promise
  authSession.applySession(owner, { ...session, user: { ...user, tenant_id: 'tenant-b' } })
  const current = authSession.getSnapshot()
  // When
  send(response, 200, { ...session, access_token: 'late-login' })
  // Then
  await assert.rejects(login, StaleSessionError)
  assert.equal(authSession.getSnapshot(), current)
  assert.equal(effects, 0)
})

test('deferred switch response cannot install a replacement for a newer login', async (t) => {
  // Given
  const arrived = Promise.withResolvers<ServerResponse>()
  const { controller } = await wire(t, (_request, response) => { arrived.resolve(response) })
  const owner = authSession.capture()
  const switching = switchTenant('tenant-b', owner).then((result) => controller.refreshUser(owner, result.access_token))
  const response = await arrived.promise
  authSession.applySession(owner, { ...session, access_token: 'new-login' })
  const current = authSession.getSnapshot()
  // When
  send(response, 200, { access_token: 'late-switch' })
  // Then
  await assert.rejects(switching, StaleSessionError)
  assert.equal(authSession.getSnapshot(), current)
})

test('same-token replacement hides the old tenant and validates using its new owner', async (t) => {
  // Given
  const arrived = Promise.withResolvers<{ response: ServerResponse; authorization: string | undefined }>()
  const { controller } = await wire(t, (request, response) => { arrived.resolve({ response, authorization: request.headers.authorization }) })
  const owner = authSession.capture()
  // When
  const refresh = controller.refreshUser(owner, 'token-a')
  const request = await arrived.promise
  // Then
  assert.notEqual(authSession.capture(), owner)
  assert.equal(authSession.getSnapshot().user, null)
  assert.equal(authSession.getSnapshot().status, 'loading')
  assert.equal(request.authorization, 'Bearer token-a')
  send(request.response, 200, { ...user, tenant_id: 'tenant-b' })
  assert.equal(await refresh, true)
  assert.equal(authSession.getSnapshot().user?.tenant_id, 'tenant-b')
})

test('late replacement validation cannot overwrite a newer session', async (t) => {
  // Given
  const arrived = Promise.withResolvers<ServerResponse>()
  const { controller } = await wire(t, (_request, response) => { arrived.resolve(response) })
  const refresh = controller.refreshUser(authSession.capture(), 'replacement')
  const response = await arrived.promise
  authSession.applySession(authSession.capture(), { ...session, access_token: 'new-login' })
  const current = authSession.getSnapshot()
  // When
  send(response, 200, { ...user, tenant_id: 'tenant-b' })
  // Then
  assert.equal(await refresh, false)
  assert.equal(authSession.getSnapshot(), current)
})

test('ordinary transient refresh preserves the validated identity and owner', async (t) => {
  // Given
  const { controller } = await wire(t, (_request, response) => send(response, 503, { detail: 'temporarily unavailable' }))
  const current = authSession.getSnapshot()
  // When
  const refreshed = await controller.refreshUser(current.owner)
  // Then
  assert.equal(refreshed, false)
  assert.equal(authSession.getSnapshot(), current)
})

test('unvalidated replacement failure ends loading and allows a new sign-in', async (t) => {
  // Given
  const { controller, localStorage } = await wire(t, (_request, response) => send(response, 503, { detail: 'temporarily unavailable' }))
  const owner = authSession.capture()
  // When
  const refreshed = await controller.refreshUser(owner, 'replacement')
  // Then
  assert.equal(refreshed, false)
  assert.equal(authSession.getSnapshot().status, 'anonymous')
  assert.equal(authSession.getSnapshot().user, null)
  assert.equal(localStorage.getItem('maraclaw-enduser-token'), null)
  assert.equal(authSession.applySession(authSession.capture(), session), true)
})

test('current 401 expires the identity and fences continuation guards', async (t) => {
  // Given
  const { controller } = await wire(t, (_request, response) => send(response, 401, { detail: 'expired' }))
  const owner = authSession.capture()
  // When
  const refreshed = await controller.refreshUser(owner)
  // Then
  assert.equal(refreshed, false)
  assert.equal(authSession.getSnapshot().status, 'anonymous')
  assert.equal(authSession.isCurrent(owner), false)
})

test('malformed canonical join and switch responses leave the validated session untouched', async (t) => {
  // Given
  await wire(t, (_request, response) => send(response, 200, { token: 'legacy-token', user }))
  const current = authSession.getSnapshot()
  // When
  const responses = await Promise.allSettled([joinDefaultOrg(current.owner), switchTenant('tenant-b', current.owner)])
  // Then
  assert.deepEqual(responses.map((result) => result.status), ['rejected', 'rejected'])
  assert.equal(authSession.getSnapshot(), current)
})

test('StrictMode-style refresh cancellation does not expire an unvalidated session', async (t) => {
  // Given
  const arrived = Promise.withResolvers<ServerResponse>()
  const { controller } = await wire(t, (_request, response) => { arrived.resolve(response) })
  const refresh = controller.refreshUser(authSession.capture(), 'replacement')
  await arrived.promise
  const current = authSession.getSnapshot()
  // When
  controller.cancelRefresh()
  // Then
  assert.equal(await refresh, false)
  assert.equal(authSession.getSnapshot(), current)
})
