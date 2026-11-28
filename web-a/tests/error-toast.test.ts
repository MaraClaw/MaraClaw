import assert from 'node:assert/strict'
import test from 'node:test'
import { toast } from 'sonner'

import { requestFailureMessage, showErrorToast } from '../src/lib/error-toast.ts'
import { ApiError, REQUEST_TIMEOUT_MESSAGE, RequestTimeoutError } from '../src/lib/http.ts'
import { message } from '../src/lib/i18n.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'

test('one request failure produces one toast across multiple presentation owners', () => {
  const failure = new ApiError(403, 'Access denied')
  showErrorToast('Could not load companies', failure)
  showErrorToast('Could not load company details', failure)
  const notifications = toast.getHistory().filter((item) => 'title' in item && ['Could not load companies', 'Could not load company details'].includes(String(item.title)))
  assert.equal(notifications.length, 1)
})

test('concurrent equal failures update the same Sonner notification', () => {
  showErrorToast('Cannot load notifications', new Error('first'))
  showErrorToast('Cannot load notifications', new Error('second'))
  assert.equal(toast.getHistory().filter((item) => 'title' in item && item.title === 'Cannot load notifications').length, 1)
})

test('stale and aborted requests do not create error notifications', () => {
  const count = toast.getHistory().length
  showErrorToast('Stale data', new StaleSessionError())
  showErrorToast('Aborted request', new DOMException('Aborted', 'AbortError'))
  assert.equal(toast.getHistory().length, count)
})

test('presentation never sends markup to Sonner and keeps domain-safe copy', () => {
  const failure = new ApiError(502, '<html>load-balancer</html>')
  showErrorToast('<html>load-balancer</html>', failure)
  showErrorToast('Check SMTP settings before sending invitations.', new Error('smtp_password=secret'))
  const titles = toast.getHistory().flatMap((item) => 'title' in item ? [item.title] : [])
  assert.ok(titles.includes('The server is temporarily unavailable. Please try again shortly.'))
  assert.ok(titles.includes('Check SMTP settings before sending invitations.'))
  assert.ok(titles.every((title) => !String(title).includes('<html>') && !String(title).includes('smtp_password')))
})

test('a timeout replaces the caller fallback with its own retry message; other failures keep the fallback', () => {
  const fallback = 'Could not load companies'
  assert.deepEqual(requestFailureMessage(new RequestTimeoutError(90_000, null), fallback), message(REQUEST_TIMEOUT_MESSAGE))
  assert.equal(requestFailureMessage(new TypeError('Failed to fetch'), fallback), fallback)
  assert.equal(requestFailureMessage(new ApiError(503, 'unavailable'), fallback), fallback)
})
