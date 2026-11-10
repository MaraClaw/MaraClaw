import assert from 'node:assert/strict'
import { test } from 'node:test'
import { toast } from 'sonner'
import { showRequestError } from '../src/lib/request-error-toast.ts'

test('request presentation reports shared failures once and permits a later request', () => {
  const message = 'Unable to load the request-toast fixture.'
  const first = new Error('upstream failed')
  const before = toast.getHistory().length
  showRequestError(message, first)
  showRequestError('A sibling surface observed the same request failure.', first)
  assert.equal(toast.getHistory().length, before + 1)
  const nextMessage = 'A later request could not be saved.'
  showRequestError(nextMessage, new Error('new request failed'))
  assert.equal(toast.getHistory().length, before + 2)
  const latest = toast.getHistory().at(-1)
  assert.ok(latest && 'title' in latest)
  assert.equal(latest.title, nextMessage)
  assert.equal(latest.id, `request-error:${nextMessage}`)
})

test('request presentation ignores retired and aborted requests', () => {
  const before = toast.getHistory().length
  for (const name of ['AbortError', 'StaleSessionError', 'RetiredWorkspaceError']) {
    const error = new Error('A request which must remain silent')
    error.name = name
    showRequestError(error.message, error)
  }
  assert.equal(toast.getHistory().length, before)
})

test('request presentation never puts proxy markup into Sonner', () => {
  showRequestError('<html><h1>502 Bad Gateway</h1>nginx</html>', new Error('proxy failed'))
  const latest = toast.getHistory().at(-1)
  assert.ok(latest && 'title' in latest)
  assert.equal(latest.type, 'error')
  assert.equal(latest.title, 'Unable to complete the request. Please try again.')
})
