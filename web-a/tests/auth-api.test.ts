import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { changePasswordRequest, fetchCurrentUser, loginRequest } from '../src/lib/auth-api.ts'
import { ownAuditSession } from './admin-audit-fixtures.ts'
import { tokenResponse } from './helpers.ts'

test('login parses the membership contract without attaching the stored admin token', async (context) => {
  // Given
  ownAuditSession(context)
  const response = tokenResponse()
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(response))
  const credentials = { login_identifier: 'operator@example.test', password: 'synthetic-password' }
  // When
  const result = await loginRequest(credentials)
  // Then
  assert.deepEqual(result, response)
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/auth/login')
  assert.deepEqual(JSON.parse(String(options?.body)), credentials)
  assert.deepEqual(options?.headers, { Accept: 'application/json', 'Content-Type': 'application/json' })
})

for (const response of [
  { access_token: 'identity-only' },
  { ...tokenResponse(), user: null },
  { ...tokenResponse(), user: { ...tokenResponse().user, role: 'super_admin' } },
  { ...tokenResponse(), user: { ...tokenResponse().user, display_name: null } },
]) {
  test('login rejects identity-only or malformed membership responses', async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(response))
    // When / Then
    await assert.rejects(loginRequest({ login_identifier: 'operator@example.test', password: 'synthetic-password' }), z.ZodError)
  })
}

test('current user preserves omitted optional fields and explicit null profile fields', async (context) => {
  // Given
  ownAuditSession(context).replace('explicit-token')
  const user = {
    id: tokenResponse().user.id, display_name: 'Operator', role: 'org_admin', is_active: true,
    created_at: '2026-10-25T00:00:00Z', email: null,
  }
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(user))
  // When
  const result = await fetchCurrentUser('explicit-token')
  // Then
  assert.deepEqual(result, user)
  assert.equal('tenant_id' in result, false)
  assert.deepEqual(fetch.mock.calls[0]?.arguments[1]?.headers, { Accept: 'application/json', Authorization: 'Bearer explicit-token' })
})

test('tenant selection retains the generated required tenant-list contract', async (context) => {
  // Given
  const selection = { requires_tenant_selection: true, login_identifier: 'operator@example.test', tenants: [] }
  context.mock.method(globalThis, 'fetch', async () => Response.json(selection))
  // When
  const result = await loginRequest({ login_identifier: 'operator@example.test', password: 'synthetic-password' })
  // Then
  assert.deepEqual(result, selection)
})

test('password change rejects a missing required password-change flag', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  // When / Then
  await assert.rejects(changePasswordRequest({ old_password: 'old', new_password: 'new' }), z.ZodError)
})
