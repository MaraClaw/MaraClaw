import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'

import { admissionAccessOptions, hasPendingAdmission, isAdmissionAccessPath, isPendingAdmissionApplicant } from '../src/lib/admission-access.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import type { AdmissionSummary } from '../src/lib/types/company-admissions.ts'
import { assertBackedOff, focusRefetch, pageVisibility, pollInterval, throttled } from './poll-fixture.ts'

const pending: AdmissionSummary = {
  id: 'pending-request', status: 'pending', source_tenant_id: 'source',
  target_tenant: { id: 'target', name: 'Company' }, method: 'invitation',
  created_at: '2026-10-25T12:00:00Z', decided_at: null, decision_reason: null, transfer: null,
}
const transfer = {
  transfer_request_id: '44444444-4444-4444-8444-444444444444', choice: 'paid', fee_amount_minor: 1000,
  currency: 'USD', free_consent_at: null,
} as const

for (const scenario of [
  { name: 'pending non-transfer', request: pending, restricted: true },
  { name: 'pending legacy omitted transfer', request: { ...pending, transfer: undefined }, restricted: true },
  { name: 'explicit transfer', request: { ...pending, transfer }, restricted: false },
  { name: 'other source', request: { ...pending, source_tenant_id: 'another-source' }, restricted: false },
  ...(['approved', 'rejected', 'withdrawn'] as const).map((status) => ({ name: status, request: { ...pending, status }, restricted: false })),
]) {
  test(`admission access identifies ${scenario.name} for the current source`, () => {
    // Given / When
    const restricted = isPendingAdmissionApplicant(scenario.request, 'source')
    // Then
    assert.equal(restricted, scenario.restricted)
  })
}

for (const path of ['/app/notifications', '/app/settings', '/app/account', '/app/admissions', '/app/admissions/request', '/app/admissions/transfers/request']) {
  test(`restricted admission access permits ${path}`, () => {
    // Given / When
    const allowed = isAdmissionAccessPath(path)
    // Then
    assert.equal(allowed, true)
  })
}

for (const path of ['/app', '/app/agents', '/app/agents/one/settings', '/app/onboarding', '/app/clawbook', '/app/okr', '/app/directory', '/app/admissions-other', '/app/settings/agent', '/app/notifications/other']) {
  test(`restricted admission access blocks ${path}`, () => {
    // Given / When
    const allowed = isAdmissionAccessPath(path)
    // Then
    assert.equal(allowed, false)
  })
}

test('pending access follows every page and finds a matching second-page applicant', async (context) => {
  // Given
  const owner = authSession.capture()
  const signal = new AbortController().signal
  const cursor = 'opaque/+?=&cursor'
  const calls: (string | null)[] = []
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    const query = new URL(path, 'https://fixture.test').searchParams
    assert.equal(query.get('status'), 'pending')
    assert.equal(query.get('limit'), '100')
    assert.equal(init.signal, signal)
    calls.push(query.get('cursor'))
    return Response.json(query.has('cursor')
      ? { items: [pending], next_cursor: 'unused-next-page' }
      : { items: [{ ...pending, source_tenant_id: 'other' }, { ...pending, transfer }], next_cursor: cursor })
  })
  // When
  const restricted = await hasPendingAdmission('source', { owner, signal })
  // Then
  assert.equal(restricted, true)
  assert.deepEqual(calls, [null, cursor])
})

test('pending access grants workspace only after exhausting pages without a match', async (context) => {
  // Given
  const calls: string[] = []
  context.mock.method(globalThis, 'fetch', async (path: string) => {
    calls.push(path)
    return Response.json({ items: [], next_cursor: calls.length === 1 ? 'next' : null })
  })
  // When
  const restricted = await hasPendingAdmission('source', { owner: authSession.capture(), signal: new AbortController().signal })
  // Then
  assert.equal(restricted, false)
  assert.equal(calls.length, 2)
})

test('pending access propagates a later-page failure instead of granting workspace', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async (path: string) => new URL(path, 'https://fixture.test').searchParams.has('cursor')
    ? Response.json({ detail: 'Unavailable' }, { status: 503 })
    : Response.json({ items: [], next_cursor: 'next' }))
  // When
  const result = hasPendingAdmission('source', { owner: authSession.capture(), signal: new AbortController().signal })
  // Then
  await assert.rejects(result, (error: unknown) => error instanceof ApiError && error.status === 503)
})

test('pending access discards completion after its captured owner retires', async (context) => {
  // Given
  const owner = authSession.capture()
  context.mock.method(globalThis, 'fetch', async () => {
    authSession.logout(owner)
    return Response.json({ items: [], next_cursor: null })
  })
  // When
  const result = hasPendingAdmission('source', { owner, signal: new AbortController().signal })
  // Then
  await assert.rejects(result, StaleSessionError)
})

for (const [answer, cadence, focus] of [[true, 30_000, 'always'], [false, false, false], [undefined, false, false]] as const) {
  test(`workspace access ${answer === undefined ? 'still loading' : answer ? 'held by a pending admission' : 'granted'} polls ${String(cadence)} and refetches on focus ${String(focus)}`, (t) => {
    // Given
    const client = new QueryClient()
    t.after(() => client.clear())
    const options = admissionAccessOptions(authSession.capture(), 'source')
    if (answer !== undefined) client.setQueryData(options.queryKey, answer)
    // When / Then
    assert.equal(pollInterval(client, options), cadence)
    assert.equal(focusRefetch(client, options), focus)
    assert.equal(options.retry, false)
  })
}

test('a pending admission is not rechecked while the page is hidden', (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const page = pageVisibility(t, 'hidden')
  const options = admissionAccessOptions(authSession.capture(), 'source')
  client.setQueryData(options.queryKey, true)
  // When / Then
  assert.equal(pollInterval(client, options), false)
  page.show()
  assert.equal(pollInterval(client, options), 30_000)
})

test('an outage backs the pending admission recheck off to the ceiling and the first answer restores 30 s', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  let healthy = false
  t.mock.method(globalThis, 'fetch', async () => healthy ? Response.json({ items: [pending], next_cursor: null }) : throttled(503))
  const options = admissionAccessOptions(authSession.capture(), 'source')
  client.setQueryData(options.queryKey, true)
  assert.equal(pollInterval(client, options), 30_000)
  // When / Then
  for (let failure = 0; failure < 3; failure++) {
    await assert.rejects(client.fetchQuery(options), ApiError)
    assertBackedOff(pollInterval(client, options), 60_000)
  }
  healthy = true
  assert.equal(await client.fetchQuery(options), true)
  assert.equal(pollInterval(client, options), 30_000)
})

test('a Retry-After on the failed admission recheck holds the poll back at least that long, never past the ceiling', async (t) => {
  // Given
  const client = new QueryClient()
  t.after(() => client.clear())
  const retryAfter = ['50', '3600']
  t.mock.method(globalThis, 'fetch', async () => throttled(503, retryAfter.shift()))
  const options = admissionAccessOptions(authSession.capture(), 'source')
  client.setQueryData(options.queryKey, true)
  assert.equal(pollInterval(client, options), 30_000)
  // When
  await assert.rejects(client.fetchQuery(options), (error: unknown) => error instanceof ApiError && error.retryAfterMs === 50_000)
  const delay = pollInterval(client, options)
  await assert.rejects(client.fetchQuery(options), (error: unknown) => error instanceof ApiError && error.retryAfterMs === 3_600_000)
  // Then
  assert.ok(typeof delay === 'number' && delay >= 50_000 && delay <= 60_000, String(delay))
  assert.equal(pollInterval(client, options), 60_000)
})
