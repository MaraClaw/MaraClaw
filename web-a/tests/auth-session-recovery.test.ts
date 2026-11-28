import assert from 'node:assert/strict'
import test from 'node:test'
import { ZodError } from 'zod'

import { createAuthSession, type ScheduleRetry } from '../src/lib/auth-session.ts'
import { TOKEN_KEY } from '../src/lib/auth-storage.ts'
import { ApiError, RequestTimeoutError } from '../src/lib/http.ts'
import type { UserOut } from '../src/lib/types/auth.ts'
import { adminUser, createFixture, tokenResponse } from './helpers.ts'

function manualRetries() {
  const scheduled: Array<{ readonly retry: () => void; readonly delayMs: number }> = []
  const schedule: ScheduleRetry = (retry, delayMs) => {
    scheduled.push({ retry, delayMs })
    return () => undefined
  }
  return { scheduled, schedule }
}

const settle = () => new Promise<void>((resolve) => { setImmediate(resolve) })

for (const [label, failure] of [
  ['a timeout', new RequestTimeoutError(90_000, null)],
  ['a 503', new ApiError(503, 'unavailable')],
  ['a network error', new TypeError('Failed to fetch')],
  ['a schema error', new ZodError([])],
] as const) {
  test(`a /me check failing with ${label} on page load keeps the stored session loading and retries with backoff`, async (t) => {
    // Given
    const { policy, storage } = createFixture()
    const { scheduled, schedule } = manualRetries()
    const answers: Array<() => Promise<UserOut>> = [
      () => Promise.reject(failure),
      () => Promise.reject(failure),
      async () => adminUser(),
    ]
    const auth = createAuthSession(policy, {
      fetchCurrentUser: () => {
        const next = answers.shift()
        assert.ok(next)
        return next()
      },
      loginRequest: async () => tokenResponse(),
    }, schedule)
    const generation = policy.capture().generation
    // When
    t.after(auth.connect())
    await settle()
    // Then
    assert.equal(storage.getItem(TOKEN_KEY), 'token-a')
    assert.equal(policy.capture().generation, generation)
    assert.equal(auth.getSnapshot().status, 'loading')
    assert.equal(auth.getSnapshot().user, null)
    // When
    scheduled[0]?.retry()
    await settle()
    // Then
    assert.equal(auth.getSnapshot().status, 'loading')
    assert.deepEqual(scheduled.map(({ delayMs }) => delayMs), [2_000, 4_000])
    // When
    scheduled[1]?.retry()
    await settle()
    // Then
    assert.equal(auth.getSnapshot().status, 'authenticated')
    assert.equal(auth.getSnapshot().user?.id, 'admin-a')
    assert.equal(answers.length, 0)
  })
}

test('a non-admin /me on page load is still rejected without a retry', async (t) => {
  // Given
  const { policy, storage } = createFixture()
  const { scheduled, schedule } = manualRetries()
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async () => ({ ...adminUser(), role: 'member', is_platform_admin: false }),
    loginRequest: async () => tokenResponse(),
  }, schedule)
  // When
  t.after(auth.connect())
  await settle()
  // Then
  assert.equal(storage.getItem(TOKEN_KEY), null)
  assert.equal(auth.getSnapshot().status, 'anonymous')
  assert.equal(scheduled.length, 0)
})

test('a timed-out /me keeps a validated admin signed in without a retry', async (t) => {
  // Given
  const { policy, storage } = createFixture()
  const { scheduled, schedule } = manualRetries()
  let timedOut = false
  const auth = createAuthSession(policy, {
    fetchCurrentUser: async () => {
      if (timedOut) throw new RequestTimeoutError(90_000, null)
      return adminUser()
    },
    loginRequest: async () => tokenResponse(),
  }, schedule)
  t.after(auth.connect())
  await auth.refreshUser()
  const retained = auth.getSnapshot()
  assert.equal(retained.status, 'authenticated')
  // When
  timedOut = true
  await auth.refreshUser()
  // Then
  assert.strictEqual(auth.getSnapshot(), retained)
  assert.equal(storage.getItem(TOKEN_KEY), 'token-a')
  assert.equal(scheduled.length, 0)
})
