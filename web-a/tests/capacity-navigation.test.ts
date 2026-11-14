import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { safeNotificationLink } from '../src/lib/notification-links.ts'
import {
  canBuyCapacity, canManageCapacityPaymentSettings, canReviewCapacityPayments, canViewCapacityOrders, type UserOut,
} from '../src/lib/types/auth.ts'
import { adminUser } from './helpers.ts'
import { capacityOrderId } from './capacity-fixtures.ts'

const orgAdmin = adminUser()
const platformAdmin: UserOut = { ...adminUser('platform-a'), role: 'platform_admin', tenant_id: null, is_platform_admin: true }
const genesisPlatformAdmin: UserOut = { ...platformAdmin, is_genesis: true }
const genesisOrgAdmin: UserOut = { ...orgAdmin, is_genesis: true }

const cases: readonly (readonly [string, UserOut | null | undefined, { buy: boolean; view: boolean; review: boolean; settings: boolean }])[] = [
  ['an active organization admin', orgAdmin, { buy: true, view: true, review: false, settings: false }],
  ['the genesis organization admin', genesisOrgAdmin, { buy: true, view: true, review: false, settings: false }],
  ['an organization admin without a company', { ...orgAdmin, tenant_id: null }, { buy: false, view: false, review: false, settings: false }],
  ['a deactivated organization admin', { ...orgAdmin, is_active: false }, { buy: false, view: false, review: false, settings: false }],
  ['an additional platform admin', platformAdmin, { buy: false, view: true, review: true, settings: false }],
  ['the genesis platform admin', genesisPlatformAdmin, { buy: false, view: true, review: true, settings: true }],
  ['a deactivated platform admin', { ...genesisPlatformAdmin, is_active: false }, { buy: false, view: false, review: false, settings: false }],
  ['an organization admin flagged as a platform admin', { ...orgAdmin, is_platform_admin: true }, { buy: false, view: true, review: true, settings: false }],
  ['a member', { ...orgAdmin, role: 'member' }, { buy: false, view: false, review: false, settings: false }],
  ['a signed-out visitor', null, { buy: false, view: false, review: false, settings: false }],
  ['an unknown user', undefined, { buy: false, view: false, review: false, settings: false }],
]

for (const [label, user, expected] of cases) {
  test(`capacity access for ${label}`, () => {
    assert.deepEqual({
      buy: canBuyCapacity(user), view: canViewCapacityOrders(user), review: canReviewCapacityPayments(user), settings: canManageCapacityPaymentSettings(user),
    }, expected)
  })
}

test('the review nav entry is distinct from Payments and gated like its route', () => {
  const shell = readFileSync(new URL('../src/components/layout/admin-shell.tsx', import.meta.url), 'utf8')
  const entries = [...(shell.match(/const navItems:[\s\S]*?=\s*\[([\s\S]*?)\n\]/)?.[1] ?? '').matchAll(/\{\s*to:\s*'([^']+)'([^{}]*)\}/g)]
  const capacity = entries.find((entry) => entry[1] === '/capacity-payments')
  const labels = entries.map((entry) => /label:\s*'([^']+)'/.exec(entry[2] ?? '')?.[1])
  assert.match(capacity?.[2] ?? '', /label:\s*'Capacity payments'/)
  assert.match(capacity?.[2] ?? '', /capacityReviewOnly:\s*true/)
  assert.equal(new Set(labels).size, labels.length)
  assert.equal(entries.some((entry) => entry[1] === '/capacity-orders'), false)
  assert.match(shell, /capacityReviewOnly && !canReviewCapacityPayments\(user\)/)
})

test('the routes use the same predicates as the nav and the order page stays out of the nav', () => {
  const routes = readFileSync(new URL('../src/routes/index.tsx', import.meta.url), 'utf8')
  assert.match(routes, /function CapacityReviewRoute\(\)[\s\S]*?canReviewCapacityPayments\(user\) \? <Outlet \/> : <Navigate to="\/" replace \/>/)
  assert.match(routes, /function CapacityOrderRoute\(\)[\s\S]*?canViewCapacityOrders\(user\) \? <Outlet \/> : <Navigate to="\/" replace \/>/)
  assert.match(routes, /<Route element=\{<CapacityReviewRoute \/>\}>\s*<Route path="capacity-payments" element=\{<CapacityPaymentsPage \/>\} \/>\s*<Route path="capacity-payments\/:orderId" element=\{<CapacityPaymentDetailPage \/>\} \/>/)
  assert.match(routes, /<Route element=\{<CapacityOrderRoute \/>\}>\s*<Route path="capacity-orders\/:orderId" element=\{<CapacityOrderPage \/>\} \/>/)
})

test('capacity notifications open only an exact order or payment id', () => {
  const links = [
    `/capacity-payments/${capacityOrderId}`, `/capacity-orders/${capacityOrderId}`, `/capacity-orders/${capacityOrderId.toUpperCase()}`,
    '/capacity-payments', '/capacity-orders', '/capacity-orders/', '/capacity-orders/not-a-uuid', `/capacity-orders/${capacityOrderId}?next=/users`,
    `/capacity-orders/${capacityOrderId}/evidence`, `/capacity-orders/../users`, `//external.test/capacity-orders/${capacityOrderId}`,
    `/capacity-other/${capacityOrderId}`, `/capacity-orders/${capacityOrderId}\n`, null,
  ]
  assert.deepEqual(links.map(safeNotificationLink), [
    links[0], links[1], links[2], null, null, null, null, null, null, null, null, null, null, null,
  ])
})

test('existing notification destinations are unchanged', () => {
  assert.equal(safeNotificationLink('/users'), '/users')
  assert.equal(safeNotificationLink('/organization-admissions'), '/organization-admissions')
  assert.equal(safeNotificationLink('/agent-deletion-requests'), '/agent-deletion-requests')
  assert.equal(safeNotificationLink('/capacity-payments-evil'), null)
})
