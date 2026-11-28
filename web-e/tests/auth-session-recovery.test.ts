import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'
import { createAuthSessionStore } from '../src/lib/auth-session.ts'
import { createAuthSessionController, type ScheduleRetry } from '../src/lib/auth-session-controller.ts'
import { ApiError, RequestTimeoutError } from '../src/lib/http.ts'
import type { UserOut } from '../src/lib/types/auth.ts'

const user: UserOut = {
  id: 'member', email: 'member@example.com', display_name: 'Member',
  role: 'member', tenant_id: 'tenant-a', email_verified: true,
}
const drain = () => new Promise<void>((resolve) => { setImmediate(resolve) })
/** A stored token that no `/me` has validated yet, as on a page load. */
function unvalidated() {
  let token: string | null = 'token-a'
  const store = createAuthSessionStore({ read: () => token, write: (value) => { token = value }, clear: () => { token = null } })
  const retries: Array<{ readonly retry: () => void; readonly delayMs: number }> = []
  let cancelled = 0
  const schedule: ScheduleRetry = (retry, delayMs) => {
    retries.push({ retry, delayMs })
    return () => { cancelled++ }
  }
  return { store, stored: () => token, retries, schedule, cancelled: () => cancelled }
}

for (const [label, failure] of [
  ['a timeout', new RequestTimeoutError(90_000, null)],
  ['a 503', new ApiError(503, 'unavailable')],
  ['a network error', new TypeError('Failed to fetch')],
  ['a schema error', new ZodError([])],
] as const) {
  test(`an unvalidated session failing with ${label} stays signed in and loading and retries with backoff`, async () => {
    // Given
    const { store, stored, retries, schedule } = unvalidated()
    const outcomes: Array<() => Promise<UserOut>> = [
      () => Promise.reject(failure),
      () => Promise.reject(failure),
      () => Promise.resolve(user),
    ]
    const controller = createAuthSessionController(store, () => {
      const next = outcomes.shift()
      assert.ok(next)
      return next()
    }, schedule)
    const owner = store.capture()
    // When
    const refreshed = await controller.refreshUser(owner)
    // Then
    assert.equal(refreshed, false)
    assert.equal(store.getSnapshot().status, 'loading')
    assert.equal(store.isCurrent(owner), true)
    assert.equal(stored(), 'token-a')
    // When
    retries[0]?.retry()
    await drain()
    // Then
    assert.equal(store.getSnapshot().status, 'loading')
    assert.deepEqual(retries.map(({ delayMs }) => delayMs), [2_000, 4_000])
    // When
    retries[1]?.retry()
    await drain()
    // Then
    assert.equal(store.getSnapshot().status, 'authenticated')
    assert.equal(store.getSnapshot().user?.id, user.id)
    assert.equal(retries.length, 2)
    assert.equal(outcomes.length, 0)
  })
}

test('an unvalidated session rejected with 401 signs out without a retry', async () => {
  // Given
  const { store, stored, retries, schedule } = unvalidated()
  const controller = createAuthSessionController(store, () => Promise.reject(Object.assign(new Error('expired'), { status: 401 })), schedule)
  // When
  const refreshed = await controller.refreshUser(store.capture())
  // Then
  assert.equal(refreshed, false)
  assert.equal(store.getSnapshot().status, 'anonymous')
  assert.equal(stored(), null)
  assert.equal(retries.length, 0)
})

test('cancelling refresh also cancels a pending retry', async () => {
  // Given
  const { store, retries, schedule, cancelled } = unvalidated()
  const controller = createAuthSessionController(store, () => Promise.reject(new RequestTimeoutError(90_000, null)), schedule)
  await controller.refreshUser(store.capture())
  assert.equal(retries.length, 1)
  // When
  controller.cancelRefresh()
  // Then
  assert.equal(cancelled(), 1)
})
