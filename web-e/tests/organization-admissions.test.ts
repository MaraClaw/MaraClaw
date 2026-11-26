import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { ApiError } from '../src/lib/http.ts'
import {
  organizationQuoteFormSchema, organizationQuoteRequestSchema, organizationRefusalCode,
  requestOrganizationQuote, submitOrganizationAdmission,
} from '../src/lib/organization-admissions-api.ts'
import { fetchOrganizationPlans } from '../src/lib/organization-plans-api.ts'
import { formatPhone, phoneCountryOptions, toE164 } from '../src/lib/phone.ts'
import { ownAccountSession } from './auth-account-fixture.ts'
import { plans, quote } from './organization-quote-fixture.ts'

const request = { organization_name: 'Example Organization', email: 'owner@example.test', phone: '+84912345678', tier: 'ultra', billing: 'annual' } as const
const receipt = () => new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'receipt.png', { type: 'image/png' })

test('catalog, quote and receipt are public even with a member session, and preserve that session', async (context) => {
  // Given
  ownAccountSession(context, { access_token: 'member-token', user: null })
  const snapshot = authSession.getSnapshot()
  const signal = new AbortController().signal
  const requests: { path: string; init: RequestInit }[] = []
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    requests.push({ path, init })
    if (path.endsWith('organization-plans')) return Response.json({ plans })
    if (path.endsWith('/quote')) return Response.json(quote)
    return Response.json({ status: 'pending', reference: quote.reference }, { status: 202 })
  })
  // When
  assert.deepEqual(await fetchOrganizationPlans(signal), { plans })
  assert.deepEqual(await requestOrganizationQuote({ ...request, organization_name: '  Example Organization  ' }), quote)
  assert.deepEqual(await submitOrganizationAdmission(quote.quote_token, receipt()), { status: 'pending', reference: quote.reference })
  // Then
  assert.equal(requests[0]?.path, '/api/organization-plans')
  assert.equal(requests[0]?.init.signal, signal)
  assert.equal(requests[1]?.path, '/api/organization-admissions/quote')
  assert.equal(requests[1]?.init.method, 'POST')
  assert.deepEqual(JSON.parse(String(requests[1]?.init.body)), request)
  assert.equal(requests[2]?.path, '/api/organization-admissions')
  assert.equal(requests[2]?.init.method, 'POST')
  for (const request of requests) assert.equal(new Headers(request.init.headers).has('Authorization'), false)
  assert.equal(authSession.getSnapshot(), snapshot)
})

test('the receipt goes out as multipart with the quote token and the file, leaving the boundary to the browser', async (context) => {
  const requests: RequestInit[] = []
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    requests.push(init)
    return Response.json({ status: 'pending', reference: quote.reference }, { status: 202 })
  })
  await submitOrganizationAdmission(quote.quote_token, receipt())
  const form = requests[0]?.body
  assert.ok(form instanceof FormData)
  assert.equal(form.get('quote_token'), quote.quote_token)
  const file = form.get('file')
  assert.ok(file instanceof File)
  assert.equal(file.name, 'receipt.png')
  assert.equal(new Headers(requests[0]?.headers).has('Content-Type'), false)
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

test('invalid email or billing is rejected before any network request', (context) => {
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json(quote))
  assert.throws(() => requestOrganizationQuote({ ...request, email: 'owner@' }), ZodError)
  assert.throws(() => requestOrganizationQuote({ ...request, billing: 'weekly' as never }), ZodError)
  assert.equal(organizationQuoteRequestSchema.safeParse({ organization_name: request.organization_name, email: request.email, phone: request.phone, tier: request.tier }).success, false)
  assert.equal(fetch.mock.callCount(), 0)
})

test('quote rejects an unexpected success body and propagates a refusal with its code', async (context) => {
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ ...quote, amount_vnd: 1.5 }))
  await assert.rejects(requestOrganizationQuote(request), ZodError)
  fetch.mock.mockImplementation(async () => Response.json({ detail: { code: 'email_registered', message: 'Sign in instead.' } }, { status: 409 }))
  await assert.rejects(requestOrganizationQuote(request), (error: unknown) => error instanceof ApiError && error.status === 409 && organizationRefusalCode(error) === 'email_registered')
})

test('receipt rejects an unexpected success body and propagates a retriable HTTP failure', async (context) => {
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({ status: 'approved', reference: quote.reference }))
  await assert.rejects(submitOrganizationAdmission(quote.quote_token, receipt()), ZodError)
  fetch.mock.mockImplementation(async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  await assert.rejects(submitOrganizationAdmission(quote.quote_token, receipt()), (error: unknown) => error instanceof ApiError && error.status === 503)
})

test('a refusal code is read from a structured detail only', () => {
  assert.equal(organizationRefusalCode(new ApiError(410, { code: 'quote_expired', message: 'Start again.' })), 'quote_expired')
  assert.equal(organizationRefusalCode(new ApiError(413, 'The file is too large.')), null)
  assert.equal(organizationRefusalCode(new ApiError(409, { message: 'No code.' })), null)
  assert.equal(organizationRefusalCode(new Error('offline')), null)
})

test('organization name is required and rejects blank or overlong names before any network request', (context) => {
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json(quote))
  assert.equal(organizationQuoteRequestSchema.safeParse({ email: request.email, phone: request.phone, tier: 'pro', billing: 'monthly' }).success, false)
  for (const organization_name of ['', ' \t\n ', 'a'.repeat(201)]) {
    assert.throws(() => requestOrganizationQuote({ ...request, organization_name }), ZodError)
  }
  assert.equal(fetch.mock.callCount(), 0)
})

test('organization names at both length boundaries are trimmed in the submitted payload', async (context) => {
  const bodies: unknown[] = []
  context.mock.method(globalThis, 'fetch', async (_path: string, init: RequestInit) => {
    bodies.push(JSON.parse(String(init.body)))
    return Response.json(quote)
  })
  for (const name of ['A', 'a'.repeat(200)]) {
    await requestOrganizationQuote({ ...request, organization_name: `  ${name}  ` })
    assert.deepEqual(bodies.at(-1), { ...request, organization_name: name })
  }
})

test('quote form normalizes a Vietnamese national phone into the wire body and keeps billing', () => {
  assert.deepEqual(organizationQuoteFormSchema.parse({
    organization_name: 'Example Organization', email: 'owner@example.test', tier: 'pro', billing: 'monthly', phone_number: '0912 345 678',
  }), { organization_name: 'Example Organization', email: 'owner@example.test', tier: 'pro', billing: 'monthly', phone: '+84912345678' })
  assert.equal(toE164('VN', '0912 345 678'), '+84912345678')
  assert.equal(toE164('US', '+84912345678'), '+84912345678')
  assert.equal(toE164('VN', '123'), null)
})

test('quote requires a wire phone and reports required or invalid form phones on the number field', () => {
  const input = { organization_name: 'Example Organization', email: 'owner@example.test', tier: 'pro', billing: 'monthly' }
  assert.equal(organizationQuoteRequestSchema.safeParse(input).success, false)
  for (const [phone_number, message] of [
    ['', 'Enter your phone number.'], ['   ', 'Enter your phone number.'], ['123', 'Enter a valid phone number.'],
  ] as const) {
    const result = organizationQuoteFormSchema.safeParse({ ...input, phone_country: 'VN', phone_number })
    assert.equal(result.success, false)
    if (!result.success) assert.deepEqual(result.error.issues.map(({ path, message }) => ({ path, message })), [{ path: ['phone_number'], message }])
  }
})

test('phone formats for display and falls back to the raw value', () => {
  assert.equal(formatPhone('+84912345678'), '+84 912 345 678')
  assert.equal(formatPhone('not a phone'), 'not a phone')
})

test('phone country names localize and sort for the current UI language', () => {
  for (const [locale, label] of [['en', 'Vietnam (+84)'], ['vi', 'Việt Nam (+84)']] as const) {
    const options = phoneCountryOptions(locale)
    assert.equal(options.find(({ country }) => country === 'VN')?.label, label)
    const collator = new Intl.Collator(locale)
    assert.deepEqual(options.map(({ label }) => label), options.map(({ label }) => label).sort(collator.compare))
  }
})
