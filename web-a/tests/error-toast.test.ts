import assert from 'node:assert/strict'
import test from 'node:test'
import { toast } from 'sonner'

import { showErrorToast } from '../src/lib/error-toast.ts'
import { ApiError } from '../src/lib/http.ts'
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
  const failure = new ApiError(502, '<html>nginx</html>')
  showErrorToast('<html>nginx</html>', failure)
  showErrorToast('Check SMTP settings before sending invitations.', new Error('smtp_password=secret'))
  const titles = toast.getHistory().flatMap((item) => 'title' in item ? [item.title] : [])
  assert.ok(titles.includes('The server is temporarily unavailable. Please try again shortly.'))
  assert.ok(titles.includes('Check SMTP settings before sending invitations.'))
  assert.ok(titles.every((title) => !String(title).includes('<html>') && !String(title).includes('smtp_password')))
})
