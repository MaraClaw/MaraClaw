import assert from 'node:assert/strict'
import test from 'node:test'

import { tenantEmailScope } from '../src/lib/tenant-email-access.ts'
import { adminUser } from './helpers.ts'

const own = '11111111-1111-4111-8111-111111111111'
const genesis = { ...adminUser(), is_genesis: true, tenant_id: own }

for (const role of ['org_admin', 'platform_admin'] as const) {
  test(`active genesis ${role} is scoped only to its own organization`, () => {
    // Given
    const user = { ...genesis, role, is_platform_admin: role === 'platform_admin' }
    // When
    const scope = tenantEmailScope(user)
    // Then
    assert.equal(scope, own)
  })
}

for (const [name, user] of [
  ['additional OA', { ...genesis, is_genesis: false }],
  ['additional PA', { ...genesis, role: 'platform_admin', is_platform_admin: true, is_genesis: false }],
  ['missing genesis claim', adminUser()],
  ['missing tenant', { ...genesis, tenant_id: null }],
  ['empty tenant', { ...genesis, tenant_id: '' }],
  ['inactive membership', { ...genesis, is_active: false }],
  ['member with platform flag', { ...genesis, role: 'member', is_platform_admin: true }],
  ['agent admin', { ...genesis, role: 'agent_admin' }],
  ['missing session', null],
  ['uninitialized session', undefined],
] as const) {
  test(`${name} has no SMTP scope`, () => {
    // Given / When
    const scope = tenantEmailScope(user)
    // Then
    assert.equal(scope, null)
  })
}
