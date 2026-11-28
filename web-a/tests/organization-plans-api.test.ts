import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import { z } from 'zod'

import { organizationPlansOptions } from '../src/hooks/organization-plan-options.ts'
import { adminSession } from '../src/lib/admin-session.ts'
import { fetchOrganizationPlans } from '../src/lib/organization-plans-api.ts'
import { MemoryStorage, assertForwardsCancellation } from './helpers.ts'
import { organizationAdmission } from './organization-admission-fixtures.ts'

function ownSignedInSession(context: TestContext) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: new MemoryStorage() } })
  adminSession.replace('synthetic-plans-token')
  context.after(() => {
    adminSession.logout()
    if (previous) Object.defineProperty(globalThis, 'window', previous)
    else Reflect.deleteProperty(globalThis, 'window')
  })
}

const plans = [{ ...organizationAdmission.plan, id: 'pro', name: 'Pro' }, organizationAdmission.plan]

test('the plan catalog is a public read that sends no credentials', async (context) => {
  // Given
  ownSignedInSession(context)
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _init?: RequestInit) =>
    Response.json({ plans }))
  const caller = new AbortController()
  const { signal } = caller
  // When
  const result = await fetchOrganizationPlans(signal)
  // Then
  const [url, init] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/organization-plans')
  assert.equal(new Headers(init?.headers).has('Authorization'), false)
  assertForwardsCancellation(init?.signal, caller)
  assert.deepEqual(result.map((plan) => [plan.id, plan.monthly_price_usd, plan.included_accounts, plan.included_agents]), [
    ['pro', 249, 15, 5], ['max', 249, 15, 5],
  ])
})

test('the plan catalog rejects a malformed plan', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () =>
    Response.json({ plans: [{ ...organizationAdmission.plan, monthly_price_usd: '249' }] }))
  // When / Then
  await assert.rejects(fetchOrganizationPlans(), z.ZodError)
})

test('a failed catalog read is not retried, so the form falls back to plan names at once', () => {
  assert.equal(organizationPlansOptions().retry, false)
})
