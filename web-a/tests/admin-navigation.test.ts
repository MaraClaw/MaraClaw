import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('Notifications follows Overview in shared navigation without a role restriction', () => {
  // Given
  const shell = readFileSync(new URL('../src/components/layout/admin-shell.tsx', import.meta.url), 'utf8')
  // When
  const configuration = shell.match(/const navItems:[\s\S]*?=\s*\[([\s\S]*?)\n\]/)?.[1]
  const entries = [...(configuration ?? '').matchAll(/\{\s*to:\s*'([^']+)'([^{}]*)\}/g)]
  const notifications = entries[1]
  // Then
  assert.equal(entries[0]?.[1], '/')
  assert.equal(notifications?.[1], '/notifications')
  assert.match(notifications?.[2] ?? '', /label:\s*'Notifications'/)
  assert.match(notifications?.[2] ?? '', /icon:\s*'notifications'/)
  assert.doesNotMatch(notifications?.[2] ?? '', /(?:platformAdminOnly|broadcastOnly|admissionReviewOnly):\s*true/)
  assert.equal(entries.filter((entry) => entry[1] === '/notifications').length, 1)
})

test('Clawbook directly follows Users and is gated to Clawbook viewers', () => {
  // Given
  const shell = readFileSync(new URL('../src/components/layout/admin-shell.tsx', import.meta.url), 'utf8')
  // When
  const configuration = shell.match(/const navItems:[\s\S]*?=\s*\[([\s\S]*?)\n\]/)?.[1]
  const entries = [...(configuration ?? '').matchAll(/\{\s*to:\s*'([^']+)'([^{}]*)\}/g)]
  const users = entries.findIndex((entry) => entry[1] === '/users')
  const clawbook = entries[users + 1]
  // Then
  assert.notEqual(users, -1)
  assert.equal(clawbook?.[1], '/clawbook')
  assert.match(clawbook?.[2] ?? '', /label:\s*'Clawbook'/)
  assert.match(clawbook?.[2] ?? '', /icon:\s*'clawbook'/)
  assert.match(clawbook?.[2] ?? '', /clawbookOnly:\s*true/)
  assert.match(shell, /if \(clawbookOnly && !canViewClawbook\(user\)\) return null/)
})

test('desktop and compact navigation share the unread count without standalone notification buttons', () => {
  // Given
  const shell = readFileSync(new URL('../src/components/layout/admin-shell.tsx', import.meta.url), 'utf8')
  // When
  const navigation = [...shell.matchAll(/<NavItems\b([^>]*)\/>/g)].map((match) => match[1] ?? '')
  // Then
  assert.equal(navigation.length, 2)
  assert.equal(navigation.filter((props) => /\bcompact\b/.test(props)).length, 1)
  for (const props of navigation) {
    assert.match(props, /\bunreadCount=\{unread\.data\?\.unread_count\}/)
  }
  assert.doesNotMatch(shell, /NotificationBell|notification-bell/)
})
