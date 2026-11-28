import assert from 'node:assert/strict'
import test from 'node:test'

import { loginRequest } from '../src/lib/auth-api.ts'
import { createAuthSession } from '../src/lib/auth-session.ts'
import { DEFAULT_REQUEST_TIMEOUT_MS, REQUEST_TIMEOUT_MESSAGE, RequestTimeoutError } from '../src/lib/http.ts'
import { loadVietnamese, message, translate } from '../src/lib/i18n.ts'
import { parseLoginError } from '../src/lib/login-error.ts'
import { adminUser, createFixture } from './helpers.ts'

test('a login held past its deadline shows the translated timeout message, not the generic sign-in copy', async (t) => {
  // Given
  const { policy } = createFixture(null)
  const generation = policy.capture().generation
  const deadline = new AbortController()
  const deadlines: number[] = []
  t.mock.method(AbortSignal, 'timeout', (timeoutMs: number) => { deadlines.push(timeoutMs); return deadline.signal })
  const started = Promise.withResolvers<AbortSignal | null | undefined>()
  t.mock.method(globalThis, 'fetch', (_input: RequestInfo | URL, init?: RequestInit) => {
    started.resolve(init?.signal)
    return new Promise<Response>(() => undefined)
  })
  const auth = createAuthSession(policy, { fetchCurrentUser: async () => adminUser(), loginRequest })
  const attempt = auth.login({ login_identifier: 'admin@example.test', password: 'test-password' }).then(
    () => assert.fail('a held login must not succeed'), (error: unknown) => error)
  const forwarded = await started.promise
  // When
  deadline.abort(new DOMException('signal timed out', 'TimeoutError'))
  const error = await attempt
  // Then
  assert.ok(error instanceof RequestTimeoutError)
  assert.deepEqual(deadlines, [DEFAULT_REQUEST_TIMEOUT_MS])
  assert.equal(forwarded?.aborted, true)
  const { formMessage, field } = parseLoginError(error)
  assert.deepEqual(formMessage, message(REQUEST_TIMEOUT_MESSAGE))
  assert.equal(field, undefined)
  assert.notDeepEqual(formMessage, parseLoginError(new TypeError('Failed to fetch')).formMessage)
  await loadVietnamese()
  assert.equal(typeof formMessage === 'string' ? formMessage : translate('vi', formMessage.key), 'Máy chủ phản hồi quá lâu. Kiểm tra kết nối rồi thử lại.')
  assert.equal(policy.capture().generation, generation)
  assert.equal(auth.getSnapshot().status, 'anonymous')
})
