import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { ApiError } from '../src/lib/http.ts'
import { fetchOrganizationPlans, organizationAdmissionSchema, requestOrganizationAdmission } from '../src/lib/organization-admissions-api.ts'
import { ownAccountSession } from './auth-account-fixture.ts'

const plans = [
  { id: 'pro', name: 'Pro', currency: 'USD', monthly_price_usd: 79, annual_price_usd: 790, included_accounts: 5, additional_account_monthly_usd: 5, max_agents_per_account: 2, included_agents: 2, additional_agent_monthly_usd: 35, storage_limit_bytes: 2147483648, max_file_size_bytes: 26214400, daily_messages_per_agent: 100, guest_memory_bytes: 2147483648, guest_millicpus: 1500, guest_cpu_shares: 1024, guest_pids_limit: 512, workspace_disk_bytes: 3221225472, storage_addon_bytes: 10737418240, storage_addon_monthly_usd: 5, workspace_disk_addon_bytes: 5368709120, workspace_disk_addon_monthly_usd: 3 },
  { id: 'max', name: 'Max', currency: 'USD', monthly_price_usd: 249, annual_price_usd: 2490, included_accounts: 15, additional_account_monthly_usd: 4, max_agents_per_account: 3, included_agents: 5, additional_agent_monthly_usd: 49, storage_limit_bytes: 5368709120, max_file_size_bytes: 52428800, daily_messages_per_agent: 200, guest_memory_bytes: 3221225472, guest_millicpus: 2000, guest_cpu_shares: 1536, guest_pids_limit: 768, workspace_disk_bytes: 5368709120, storage_addon_bytes: 10737418240, storage_addon_monthly_usd: 5, workspace_disk_addon_bytes: 5368709120, workspace_disk_addon_monthly_usd: 3 },
  { id: 'ultra', name: 'Ultra', currency: 'USD', monthly_price_usd: 699, annual_price_usd: 6990, included_accounts: 40, additional_account_monthly_usd: 3, max_agents_per_account: 5, included_agents: 10, additional_agent_monthly_usd: 79, storage_limit_bytes: 10737418240, max_file_size_bytes: 104857600, daily_messages_per_agent: 500, guest_memory_bytes: 5368709120, guest_millicpus: 3000, guest_cpu_shares: 2048, guest_pids_limit: 1024, workspace_disk_bytes: 8589934592, storage_addon_bytes: 10737418240, storage_addon_monthly_usd: 5, workspace_disk_addon_bytes: 5368709120, workspace_disk_addon_monthly_usd: 3 },
]

test('catalog and admission are public even with a member session, and preserve that session', async (context) => {
  // Given
  ownAccountSession(context, { access_token: 'member-token', user: null })
  const snapshot = authSession.getSnapshot()
  const signal = new AbortController().signal
  const requests: { path: string; init: RequestInit }[] = []
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    requests.push({ path, init })
    return path.endsWith('organization-plans')
      ? Response.json({ plans })
      : Response.json({ status: 'pending' }, { status: 202 })
  })
  // When
  assert.deepEqual(await fetchOrganizationPlans(signal), { plans })
  assert.deepEqual(await requestOrganizationAdmission({ organization_name: '  Example Organization  ', email: 'owner@example.test', tier: 'ultra' }), { status: 'pending' })
  // Then
  assert.equal(requests[0]?.path, '/api/organization-plans')
  assert.equal(requests[0]?.init.signal, signal)
  assert.equal(requests[1]?.path, '/api/organization-admissions')
  assert.equal(requests[1]?.init.method, 'POST')
  assert.deepEqual(JSON.parse(String(requests[1]?.init.body)), { organization_name: 'Example Organization', email: 'owner@example.test', tier: 'ultra' })
  for (const request of requests) assert.equal(new Headers(request.init.headers).has('Authorization'), false)
  assert.equal(authSession.getSnapshot(), snapshot)
})

for (const body of [
  { plans: [] },
  { plans: [plans[0], plans[0], plans[0]] },
  { plans: plans.map((plan) => ({ ...plan, currency: 'EUR' })) },
  { plans: plans.map((plan) => ({ ...plan, included_accounts: '5' })) },
  { plans: plans.map((plan) => ({ ...plan, max_accounts: 25 })) },
  { plans: plans.map((plan) => ({ ...plan, max_agents_per_account: undefined })) },
  { plans: plans.map((plan) => ({ ...plan, max_agents_per_account: '5' })) },
  { plans: plans.map((plan) => ({ ...plan, storage_limit_bytes: undefined })) },
  { plans: plans.map((plan) => ({ ...plan, storage_limit_bytes: '1073741824' })) },
  { plans: plans.map((plan) => ({ ...plan, max_file_size_bytes: undefined })) },
  { plans: plans.map((plan) => ({ ...plan, max_file_size_bytes: '26214400' })) },
  { plans: plans.map((plan) => ({ ...plan, annual_price_usd: undefined })) },
  { plans: plans.map((plan) => ({ ...plan, annual_price_usd: 588 })) },
  { plans: plans.map((plan) => ({ ...plan, included_agents: undefined })) },
  { plans: plans.map((plan) => ({ ...plan, additional_agent_monthly_usd: '35' })) },
  { plans: plans.map((plan) => ({ ...plan, storage_addon_bytes: undefined })) },
  { plans: plans.map((plan) => ({ ...plan, workspace_disk_addon_monthly_usd: '3' })) },
  { plans: plans.map(({ max_agents_per_account, ...plan }) => ({ ...plan, max_digital_employees: max_agents_per_account })) },
]) {
  test(`catalog rejects malformed allocations: ${JSON.stringify(body)}`, async (context) => {
    context.mock.method(globalThis, 'fetch', async () => Response.json(body))
    await assert.rejects(fetchOrganizationPlans(), ZodError)
  })
}

test('invalid email is rejected before any network request', (context) => {
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'pending' }))
  assert.throws(() => requestOrganizationAdmission({ organization_name: 'Example Organization', email: 'owner@', tier: 'pro' }), ZodError)
  assert.equal(fetch.mock.callCount(), 0)
})

test('repeated admission requests retain the same generic pending result', async (context) => {
  context.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'pending' }, { status: 202 }))
  const input = { organization_name: 'Example Organization', email: 'owner@example.test', tier: 'max' } as const
  assert.deepEqual(await requestOrganizationAdmission(input), await requestOrganizationAdmission(input))
})

test('admission rejects unexpected success bodies and propagates a retriable HTTP failure', async (context) => {
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'approved' }))
  const input = { organization_name: 'Example Organization', email: 'owner@example.test', tier: 'pro' } as const
  await assert.rejects(requestOrganizationAdmission(input), ZodError)
  fetch.mock.mockImplementation(async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  await assert.rejects(requestOrganizationAdmission(input), (error: unknown) => error instanceof ApiError && error.status === 503)
})

test('organization name is required and rejects blank or overlong names before any network request', (context) => {
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'pending' }))
  assert.equal(organizationAdmissionSchema.safeParse({ email: 'owner@example.test', tier: 'pro' }).success, false)
  for (const organization_name of ['', ' \t\n ', 'a'.repeat(201)]) {
    assert.throws(() => requestOrganizationAdmission({ organization_name, email: 'owner@example.test', tier: 'pro' }), ZodError)
  }
  assert.equal(fetch.mock.callCount(), 0)
})

test('organization names at both length boundaries are trimmed in the submitted payload', async (context) => {
  const bodies: unknown[] = []
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    bodies.push(JSON.parse(String(init.body)))
    return Response.json({ status: 'pending' }, { status: 202 })
  })
  for (const name of ['A', 'a'.repeat(200)]) {
    await requestOrganizationAdmission({ organization_name: `  ${name}  `, email: 'owner@example.test', tier: 'pro' })
    assert.deepEqual(bodies.at(-1), { organization_name: name, email: 'owner@example.test', tier: 'pro' })
  }
})
