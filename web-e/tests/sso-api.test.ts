import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authorizeProvider, bindProvider, oauthCallback } from '../src/lib/auth-api.ts'

const redirectUri = 'https://app.example.test/sso/callback'
const user = {
  id: '11111111-1111-4111-8111-111111111111', email: 'member@example.test', display_name: 'Member',
  tenant_id: '11111111-1111-4111-8111-111111111111', role: 'member', email_verified: true, is_active: true,
  created_at: '2026-10-25T08:00:00Z',
}

for (const intent of ['sign_in', 'bind'] as const) {
  test(`authorize for ${intent} sends no client state and returns the server-issued one`, async (context) => {
    // Given an anonymous authorize endpoint that mints its own state
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      const url = new URL(path, 'https://fixture.test')
      assert.equal(url.pathname, '/api/auth/example%2Fprovider/authorize')
      assert.deepEqual([...url.searchParams], [['redirect_uri', redirectUri], ['intent', intent]])
      assert.equal(init.method ?? 'GET', 'GET')
      assert.equal(new Headers(init.headers).has('Authorization'), false)
      return Response.json({ authorization_url: 'https://idp.example.test/auth?state=server-state', state: 'server-state' })
    })
    // When
    const result = await authorizeProvider('example/provider', redirectUri, intent)
    // Then
    assert.deepEqual(result, { authorization_url: 'https://idp.example.test/auth?state=server-state', state: 'server-state' })
  })
}

test('authorize rejects a response without a server state', async (context) => {
  // Given an engine that omits the state
  context.mock.method(globalThis, 'fetch', async () => Response.json({ authorization_url: 'https://idp.example.test/auth' }))
  // When / Then
  await assert.rejects(authorizeProvider('example', redirectUri, 'sign_in'), ZodError)
})

test('the code step posts the stored state beside the code, and the tenant step posts neither', async (context) => {
  // Given
  const bodies: unknown[] = []
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    bodies.push(JSON.parse(String(init.body)))
    return Response.json({ access_token: 'member-token', user })
  })
  // When
  await oauthCallback('example', { code: 'code', state: 'stored-state' })
  await oauthCallback('example', { pending_token: 'pending', tenant_id: user.tenant_id })
  // Then
  assert.deepEqual(bodies, [{ code: 'code', state: 'stored-state' }, { pending_token: 'pending', tenant_id: user.tenant_id }])
})

test('bind posts the stored state with the code', async (context) => {
  // Given
  let body: unknown
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    body = JSON.parse(String(init.body))
    return Response.json(user)
  })
  // When
  await bindProvider('example', 'bind-code', 'stored-state')
  // Then
  assert.deepEqual(body, { provider_type: 'example', code: 'bind-code', state: 'stored-state' })
})
