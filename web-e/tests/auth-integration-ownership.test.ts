import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { createAuthSessionController } from '../src/lib/auth-session-controller.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const user: UserOut = {
  id: 'member', email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}

test('replacement continuation suppresses effects when another owner is queued after validation commit', async (t) => {
  // Given
  let token: string | null = null
  const store = createAuthSessionStore({
    read: () => token,
    write: (next) => { token = next },
    clear: () => { token = null },
  })
  store.applySession(store.capture(), { access_token: 'original', user })
  const pending = Promise.withResolvers<UserOut>()
  const controller = createAuthSessionController(store, () => pending.promise)
  const unsubscribe = store.subscribe(() => {
    const snapshot = store.getSnapshot()
    if (snapshot.status === 'authenticated' && snapshot.user?.tenant_id === 'tenant-b') {
      queueMicrotask(() => {
        store.applySession(snapshot.owner, { access_token: 'unrelated', user: { ...user, tenant_id: 'tenant-c' } })
      })
    }
  })
  t.after(unsubscribe)
  const refresh = controller.refreshReplacement(store.capture(), 'replacement')
  const replacementOwner = store.capture()
  let effects = 0
  const continuation = refresh.then((installedOwner) => {
    if (installedOwner && store.isCurrent(installedOwner)) effects++
    return installedOwner
  })
  // When
  pending.resolve({ ...user, tenant_id: 'tenant-b' })
  // Then
  assert.equal(await continuation, replacementOwner)
  assert.equal(store.getSnapshot().user?.tenant_id, 'tenant-c')
  assert.equal(store.isCurrent(replacementOwner), false)
  assert.equal(effects, 0)
})

test('replacement validation failure cannot supply an owner for success effects', async () => {
  // Given
  let token: string | null = null
  const store = createAuthSessionStore({
    read: () => token,
    write: (next) => { token = next },
    clear: () => { token = null },
  })
  store.applySession(store.capture(), { access_token: 'original', user })
  const controller = createAuthSessionController(store, async () => { throw new TypeError('network failure') })
  // When
  const installedOwner = await controller.refreshReplacement(store.capture(), 'replacement')
  // Then
  assert.equal(installedOwner, null)
  assert.equal(store.getSnapshot().status, 'anonymous')
})
