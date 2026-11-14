import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { isPlatformAdminUser } from '../src/lib/types/auth.ts'
import { adminUser } from './helpers.ts'

const source = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('Upgrade signals is a platform-admin-only navigation entry shared by both navs', () => {
  // Given
  const shell = source('../src/components/layout/admin-shell.tsx')
  // When
  const configuration = shell.match(/const navItems:[\s\S]*?=\s*\[([\s\S]*?)\n\]/)?.[1]
  const entries = [...(configuration ?? '').matchAll(/\{\s*to:\s*'([^']+)'([^{}]*)\}/g)]
  const upgrade = entries.filter((entry) => entry[1] === '/upgrade-signals')
  // Then
  assert.equal(upgrade.length, 1)
  assert.match(upgrade[0]?.[2] ?? '', /label:\s*'Upgrade signals'/)
  assert.match(upgrade[0]?.[2] ?? '', /platformAdminOnly:\s*true/)
  assert.doesNotMatch(upgrade[0]?.[2] ?? '', /(?:broadcastOnly|invitationsOnly|admissionReviewOnly|agentDeletionReviewOnly|paymentReviewOnly):\s*true/)
  assert.match(shell, /if \(platformAdminOnly && !platformAdmin\) return null/)
})

test('the route sits inside the platform-admin gate and nowhere else', () => {
  // Given
  const routes = source('../src/routes/index.tsx')
  // When
  const gated = routes.match(/<Route element=\{<PlatformAdminRoute \/>\}>([\s\S]*?)<\/Route>/)?.[1] ?? ''
  // Then
  assert.match(gated, /<Route path="upgrade-signals" element=\{<UpgradeSignalsPage \/>\} \/>/)
  assert.equal(routes.match(/path="upgrade-signals"/g)?.length, 1)
  assert.match(source('../src/routes/platform-admin.tsx'), /isPlatformAdminUser\(user\)[\s\S]*<Navigate to="\/" replace \/>/)
})

test('an organization admin fails the gate while platform admins pass by role or flag', () => {
  // Given
  const organizationAdmin = adminUser('org-admin')
  // When / Then
  assert.equal(isPlatformAdminUser(organizationAdmin), false)
  assert.equal(isPlatformAdminUser({ ...organizationAdmin, role: 'platform_admin' as const }), true)
  assert.equal(isPlatformAdminUser({ ...organizationAdmin, is_platform_admin: true }), true)
  assert.equal(isPlatformAdminUser(null), false)
})
