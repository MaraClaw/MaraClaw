import assert from 'node:assert/strict'
import { test } from 'node:test'

import { canRequestAgentDeletion, isAgentExpired, lifecycleStartBlock } from '../src/lib/agent-lifecycle-policy.ts'
import { lifecycle } from './agent-lifecycle-fixture.ts'

for (const offset of [-1, 0, 1]) {
  test(`timestamp expiry includes equality at offset ${offset}`, () => {
    // Given
    const now = Date.parse('2030-01-01T00:00:00Z')
    const agent = { is_expired: false, expires_at: new Date(now + offset).toISOString() }
    // When / Then
    assert.equal(isAgentExpired(agent, now), offset <= 0)
  })
}

test('an explicit expiry flag wins over a future or absent timestamp', () => {
  // Given / When / Then
  assert.equal(isAgentExpired({ is_expired: true, expires_at: '2099-01-01T00:00:00Z' }), true)
  assert.equal(isAgentExpired({ is_expired: true }), true)
  assert.equal(isAgentExpired({ is_expired: false }), false)
})

for (const rights of [
  { role: 'member', creator: true, access: 'use', system: false, allowed: true },
  { role: 'org_admin', creator: false, access: 'manage', system: false, allowed: true },
  { role: 'org_admin', creator: false, access: 'use', system: false, allowed: false },
  { role: 'member', creator: false, access: 'manage', system: false, allowed: false },
  { role: 'member', creator: false, access: 'use', system: false, allowed: false },
  { role: 'org_admin', creator: true, access: 'manage', system: true, allowed: false },
]) {
  test(`deletion rights: ${JSON.stringify(rights)}`, () => {
    // Given
    const agent = { creator_id: 'creator', access_level: rights.access, is_system: rights.system }
    const user = { id: rights.creator ? 'creator' : 'other', role: rights.role }
    // When / Then
    assert.equal(canRequestAgentDeletion(agent, user), rights.allowed)
    assert.equal(canRequestAgentDeletion(agent, null), false)
  })
}

const blocked = { availability: { ...lifecycle.availability, can_start: false } }
for (const [label, input, expected] of [
  ['an expired agent during an upgrade', { expired: true, upgrading: true, confirmed: true, lifecycle }, 'expired'],
  ['an upgrade with a confirmed start', { expired: false, upgrading: true, confirmed: true, lifecycle }, 'upgrading'],
  ['an unconfirmed lifecycle', { expired: false, upgrading: false, confirmed: false, lifecycle }, 'unconfirmed'],
  ['no lifecycle result', { expired: false, upgrading: false, confirmed: true, lifecycle: undefined }, 'unconfirmed'],
  ['a confirmed lifecycle that cannot start', { expired: false, upgrading: false, confirmed: true, lifecycle: blocked }, 'unavailable'],
  ['a confirmed lifecycle that can start', { expired: false, upgrading: false, confirmed: true, lifecycle }, null],
] as const) {
  test(`start block for ${label} is ${expected}`, () => {
    // Given / When / Then
    assert.equal(lifecycleStartBlock(input), expected)
  })
}
