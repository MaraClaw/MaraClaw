import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { ApiError } from '../src/lib/http.ts'
import { safeNotificationLink } from '../src/lib/notification-links.ts'
import { decideOrganizationAdmission, fetchOrganizationAdmissions, retryOrganizationAdmissionEmail } from '../src/lib/organization-admissions-api.ts'
import { canReviewAdmissions, canReviewOrganizationAdmissions } from '../src/lib/types/auth.ts'
import { adminUser } from './helpers.ts'
import { approvedOrganizationAdmission, organizationAdmission } from './organization-admission-fixtures.ts'

test('organization review access requires an active genesis platform administrator', () => {
  const genesisPlatform = { ...adminUser(), role: 'platform_admin', is_genesis: true } as const
  const cases = [
    [genesisPlatform, true], [{ ...genesisPlatform, is_genesis: false }, false],
    [{ ...genesisPlatform, is_active: false }, false], [{ ...genesisPlatform, role: 'org_admin' }, false],
    [{ ...genesisPlatform, role: 'member' }, false], [null, false], [undefined, false],
    [{ ...genesisPlatform, role: 'org_admin', is_platform_admin: true }, false],
  ] as const
  assert.deepEqual(cases.map(([user]) => canReviewOrganizationAdmissions(user)), cases.map(([, expected]) => expected))
  assert.equal(canReviewAdmissions({ ...adminUser(), is_genesis: true }), true)
  assert.equal(canReviewAdmissions(genesisPlatform), false)
})

for (const status of ['pending', 'approved', 'rejected'] as const) {
  test(`${status} organization queue sends offset pagination and parses the quoted plan`, async (t) => {
    const page = { items: [{ ...organizationAdmission, status }], has_more: true }
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(page))
    const controller = new AbortController()
    const result = await fetchOrganizationAdmissions(status, 50, controller.signal)
    assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/admin/organization-admissions?status=${status}&limit=50&offset=50`)
    assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal, controller.signal)
    assert.deepEqual(result, page)
    assert.equal(Object.isFrozen(result.items[0]?.plan), true)
  })
}

test('organization listing begins at offset zero', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [], has_more: false }))
  await fetchOrganizationAdmissions('pending')
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/admin/organization-admissions?status=pending&limit=50&offset=0')
})

test('organization queue accepts backend-valid international email addresses', async (t) => {
  const item = { ...organizationAdmission, email: 'ü@example.com' }
  t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [item], has_more: false }))
  assert.equal((await fetchOrganizationAdmissions('pending')).items[0]?.email, item.email)
})

test('organization queue preserves legacy null names without inventing a name', async (t) => {
  const item = { ...organizationAdmission, organization_name: null }
  t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [item], has_more: false }))
  assert.equal((await fetchOrganizationAdmissions('pending')).items[0]?.organization_name, null)
})

for (const allocation of [6, 15]) {
  test(`organization queue rejects obsolete organization allocation ${allocation}`, async (t) => {
    const { max_agents_per_account: _cap, ...plan } = organizationAdmission.plan
    const legacyPlan = { ...plan, max_digital_employees: allocation }
    t.mock.method(globalThis, 'fetch', async () => Response.json({ items: [{ ...organizationAdmission, plan: legacyPlan }], has_more: false }))
    await assert.rejects(fetchOrganizationAdmissions('pending'), z.ZodError)
  })
}

for (const invalid of [
  { items: [], has_more: 'yes' }, { items: [] },
  { items: [{ ...organizationAdmission, tier: 'enterprise' }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, currency: 'EUR' } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, monthly_price_usd: '249' } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, storage_limit_bytes: undefined } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, storage_limit_bytes: '1073741824' } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, max_file_size_bytes: undefined } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, max_file_size_bytes: '26214400' } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, annual_price_usd: undefined } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, annual_price_usd: 588 } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, included_agents: undefined } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, additional_agent_monthly_usd: '49' } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, storage_addon_bytes: undefined } }], has_more: false },
  { items: [{ ...organizationAdmission, plan: { ...organizationAdmission.plan, workspace_disk_addon_monthly_usd: '3' } }], has_more: false },
  { items: [{ ...organizationAdmission, created_at: 'yesterday' }], has_more: false },
  { items: [{ ...organizationAdmission, organization_name: undefined }], has_more: false },
  { items: [{ ...organizationAdmission, organization_name: 17 }], has_more: false },
  { items: [{ ...organizationAdmission, credential_email_status: undefined }], has_more: false },
  { items: [{ ...organizationAdmission, credential_email_status: 'delivered' }], has_more: false },
  { items: [{ ...organizationAdmission, credential_email_error: undefined }], has_more: false },
  { items: [{ ...organizationAdmission, credential_email_error: 17 }], has_more: false },
]) {
  test('organization listing rejects malformed responses at the transport boundary', async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json(invalid))
    await assert.rejects(fetchOrganizationAdmissions('pending'), z.ZodError)
  })
}

for (const decision of ['approve', 'reject'] as const) {
  test(`${decision} sends a bounded optional note and parses the terminal item`, async (t) => {
    const response = { ...approvedOrganizationAdmission, status: decision === 'approve' ? 'approved' : 'rejected' }
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(response))
    const result = await decideOrganizationAdmission(organizationAdmission.id, { decision, note: 'x'.repeat(1000) })
    assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/admin/organization-admissions/${organizationAdmission.id}/decision`)
    assert.equal(fetch.mock.calls[0]?.arguments[1]?.method, 'POST')
    assert.equal(fetch.mock.calls[0]?.arguments[1]?.body, JSON.stringify({ decision, note: 'x'.repeat(1000) }))
    assert.equal(result.status, response.status)
  })
}

test('organization decisions omit an absent optional note', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(approvedOrganizationAdmission))
  await decideOrganizationAdmission(organizationAdmission.id, { decision: 'approve' })
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.body, '{"decision":"approve"}')
})

test('overlong organization review notes fail before transport', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(approvedOrganizationAdmission))
  await assert.rejects(decideOrganizationAdmission(organizationAdmission.id, { decision: 'approve', note: 'x'.repeat(1001) }), z.ZodError)
  assert.equal(fetch.mock.callCount(), 0)
})

test('organization decisions preserve typed conflicts', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Already reviewed' }, { status: 409 }))
  await assert.rejects(decideOrganizationAdmission(organizationAdmission.id, { decision: 'reject' }), (error) => error instanceof ApiError && error.status === 409)
})

test('organization decision responses must contain a complete quoted snapshot', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({ ...approvedOrganizationAdmission, plan: null }))
  await assert.rejects(decideOrganizationAdmission(organizationAdmission.id, { decision: 'approve' }), z.ZodError)
})

test('organization notifications open only the dedicated local queue', () => {
  const paths = ['/organization-admissions', '/organization-admissions?status=pending', '/organization-admissions/../users', '//external.test/organization-admissions', '/organization-admissions/unknown']
  assert.deepEqual(paths.map(safeNotificationLink), ['/organization-admissions', '/organization-admissions?status=pending', null, null, null])
})

for (const status of ['pending', 'sent', 'failed'] as const) {
  test(`credential retry parses ${status} delivery and sends no request body`, async (t) => {
    const item = { ...approvedOrganizationAdmission, credential_email_status: status, credential_email_error: status === 'failed' ? 'smtp_unavailable' : null }
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(item))
    const result = await retryOrganizationAdmissionEmail(item.id)
    assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/admin/organization-admissions/${item.id}/retry-email`)
    assert.equal(fetch.mock.calls[0]?.arguments[1]?.method, 'POST')
    assert.equal(fetch.mock.calls[0]?.arguments[1]?.body, undefined)
    assert.deepEqual(result, item)
  })
}

test('credential retry retains server refusal for obsolete credentials without retrying', async (t) => {
  const message = 'The administrator has changed the password. Original credentials cannot be resent.'
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: message }, { status: 409 }))
  await assert.rejects(retryOrganizationAdmissionEmail(organizationAdmission.id), (error) => error instanceof ApiError && error.status === 409 && error.message === message)
  assert.equal(fetch.mock.callCount(), 1)
})
