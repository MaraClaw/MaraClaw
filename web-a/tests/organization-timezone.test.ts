import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { updateMyTimezone } from '../src/lib/auth-api.ts'
import { ApiError } from '../src/lib/http.ts'
import { canManageOrganizationTimezone, organizationTimezoneError } from '../src/lib/organization-timezone.ts'
import { vietnamese } from '../src/lib/translations.ts'
import { timezoneLabel, timezoneOptions } from '../src/lib/timezone-catalog.ts'
import type { UserOut } from '../src/lib/types/auth.ts'
import { tokenResponse } from './helpers.ts'

const genesisAdmin: UserOut = { ...tokenResponse().user, role: 'org_admin', is_genesis: true, is_active: true, is_platform_admin: false }

test('only an active genesis org admin with a tenant can manage the organization timezone', () => {
  // Given / When / Then
  assert.equal(canManageOrganizationTimezone(genesisAdmin), true)
  assert.equal(canManageOrganizationTimezone({ ...genesisAdmin, is_genesis: false }), false)
  assert.equal(canManageOrganizationTimezone({ ...genesisAdmin, is_genesis: undefined }), false)
  assert.equal(canManageOrganizationTimezone({ ...genesisAdmin, role: 'platform_admin', tenant_id: null }), false)
  assert.equal(canManageOrganizationTimezone({ ...genesisAdmin, role: 'platform_admin' }), false)
  assert.equal(canManageOrganizationTimezone({ ...genesisAdmin, is_platform_admin: true }), false)
  assert.equal(canManageOrganizationTimezone({ ...genesisAdmin, is_active: false }), false)
  assert.equal(canManageOrganizationTimezone({ ...genesisAdmin, tenant_id: null }), false)
  assert.equal(canManageOrganizationTimezone({ ...genesisAdmin, role: 'member' }), false)
  assert.equal(canManageOrganizationTimezone(null), false)
})

test('updateMyTimezone patches the current user with only the timezone and returns the parsed user', async (t) => {
  // Given
  const saved = { ...genesisAdmin, timezone: 'Asia/Ho_Chi_Minh', extra: 'kept' }
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(saved))
  // When
  const result = await updateMyTimezone('Asia/Ho_Chi_Minh')
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/auth/me')
  assert.equal(options?.method, 'PATCH')
  assert.deepEqual(JSON.parse(String(options?.body)), { timezone: 'Asia/Ho_Chi_Minh' })
  assert.deepEqual(result, saved)
})

test('updateMyTimezone rejects a malformed user response instead of trusting it', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...genesisAdmin, timezone: 7 }))
  // When / Then
  await assert.rejects(updateMyTimezone('Asia/Ho_Chi_Minh'), z.ZodError)
})

test('updateMyTimezone surfaces an invalid timezone as a 422 ApiError', async (t) => {
  // Given
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Invalid timezone' }, { status: 422 }))
  // When / Then
  await assert.rejects(updateMyTimezone('Mars/Olympus'), (error) => error instanceof ApiError && error.status === 422)
})

test('a 422 and any other failure map to different catalog messages', () => {
  // Given
  const invalid = organizationTimezoneError(new ApiError(422, 'Invalid timezone'))
  const other = organizationTimezoneError(new ApiError(503, null))
  // When / Then
  assert.notEqual(invalid, other)
  assert.ok(Object.hasOwn(vietnamese, invalid))
  assert.ok(Object.hasOwn(vietnamese, other))
})

test('timezone options are the whole-hour UTC offsets, with Etc/GMT values and their sign inverted', () => {
  // Given
  const options = timezoneOptions('Etc/GMT-7')
  // When / Then
  assert.equal(options.length, 27)
  assert.deepEqual(options.find((option) => option.value === 'UTC'), { value: 'UTC', offset: 0, label: 'GMT +0' })
  assert.equal(timezoneLabel('Etc/GMT-7'), 'GMT +7')
  assert.equal(timezoneLabel('Etc/GMT+5'), 'GMT -5')
})

test('timezone options keep a saved value outside the catalog exactly once', () => {
  // Given
  const options = timezoneOptions('Asia/Ho_Chi_Minh')
  // When / Then
  assert.equal(options.length, 28)
  assert.deepEqual(options.at(-1), { value: 'Asia/Ho_Chi_Minh', label: 'Asia/Ho_Chi_Minh' })
  assert.equal(timezoneLabel('Asia/Ho_Chi_Minh'), 'Asia/Ho_Chi_Minh')
})
