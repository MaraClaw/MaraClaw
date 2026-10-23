import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'

import { adminSession } from '../src/lib/admin-session.ts'
import {
  ChatGPTSubscriptionError, completeChatGPTSubscription, getChatGPTSubscriptionStatus,
  startChatGPTSubscription,
} from '../src/lib/chatgpt-subscription-api.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { DeferredBodyResponse, MemoryStorage } from './helpers.ts'

const base = '/api/enterprise/llm-models/chatgpt-subscription'
const started = {
  attempt_id: 'attempt-one', authorization_url: 'https://auth.example.test/authorize?state=synthetic',
  redirect_uri: 'http://localhost:1455/auth/callback', expires_in: 600,
}
const callback = 'http://localhost:1455/auth/callback?code=synthetic-private-code&state=synthetic-state'

function ownSession(context: TestContext) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: new MemoryStorage() } })
  adminSession.replace('synthetic-chatgpt-token')
  context.after(() => {
    adminSession.logout()
    if (previous) Object.defineProperty(globalThis, 'window', previous)
    else Reflect.deleteProperty(globalThis, 'window')
  })
}

for (const tenant of [undefined, 'tenant/a &?#=']) {
  test(`start sends a bodyless authenticated POST ${tenant ? 'with encoded tenant scope' : 'without an org-admin tenant override'}`, async (context) => {
    // Given
    ownSession(context)
    const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(started))
    // When
    const result = await startChatGPTSubscription(tenant)
    // Then
    const [url, options] = fetch.mock.calls[0]?.arguments ?? []
    assert.equal(url, `${base}/start${tenant ? '?tenant_id=tenant%2Fa%20%26%3F%23%3D' : ''}`)
    assert.equal(options?.method, 'POST')
    assert.equal(options?.body, undefined)
    assert.deepEqual(options?.headers, { Accept: 'application/json', Authorization: 'Bearer synthetic-chatgpt-token' })
    assert.deepEqual(result, started)
  })
}

test('complete sends the full callback only in the exact POST body', async (context) => {
  // Given
  ownSession(context)
  const status = { status: 'authorized', attempt_id: started.attempt_id, model_id: 'model-one', detail: null, expires_in: null }
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(status))
  // When
  const result = await completeChatGPTSubscription({ attempt_id: started.attempt_id, callback_url: callback })
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `${base}/complete`)
  assert.equal(options?.method, 'POST')
  assert.equal(options?.body, JSON.stringify({ attempt_id: 'attempt-one', callback_url: callback }))
  assert.deepEqual(options?.headers, { Accept: 'application/json', Authorization: 'Bearer synthetic-chatgpt-token', 'Content-Type': 'application/json' })
  assert.deepEqual(result, status)
  assert.equal(fetch.mock.callCount(), 1)
})

test('status is a bodyless read-only GET scoped only by encoded attempt_id', async (context) => {
  // Given
  ownSession(context)
  const status = { status: 'pending', attempt_id: 'attempt/a &?#=' }
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json(status))
  // When
  const result = await getChatGPTSubscriptionStatus(status.attempt_id)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, `${base}/status?attempt_id=attempt%2Fa+%26%3F%23%3D`)
  assert.equal(options?.method, 'GET')
  assert.equal(options?.body, undefined)
  assert.deepEqual(result, status)
})

for (const status of ['pending', 'authorized', 'expired', 'denied', 'error']) {
  test(`complete parses the ${status} outcome without device-code fields`, async (context) => {
    // Given
    ownSession(context)
    context.mock.method(globalThis, 'fetch', async () => Response.json({ status, attempt_id: 'attempt-one', model_id: null, detail: null, expires_in: 0, user_code: 'old-device-code' }))
    // When
    const result = await completeChatGPTSubscription({ attempt_id: 'attempt-one', callback_url: callback })
    // Then
    assert.deepEqual(result, { status, attempt_id: 'attempt-one', model_id: null, detail: null, expires_in: 0 })
  })
}

for (const status of [400, 403, 409, 500]) {
  test(`complete preserves HTTP ${status} classification without retaining an echoed callback`, async (context) => {
    // Given
    ownSession(context)
    context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: callback }, { status }))
    // When / Then
    await assert.rejects(completeChatGPTSubscription({ attempt_id: 'attempt-one', callback_url: callback }), (error) => {
      assert.ok(error instanceof ChatGPTSubscriptionError)
      assert.equal(error.status, status)
      assert.equal(JSON.stringify(error).includes('synthetic-private-code'), false)
      assert.equal(String(error).includes('synthetic-private-code'), false)
      assert.equal(Object.hasOwn(error, 'cause'), false)
      return true
    })
  })
}

for (const response of [
  { session_id: 'old-device-session', verification_url: 'https://example.test', user_code: '1234', expires_in: 600, interval: 5 },
  { ...started, authorization_url: 'javascript:alert(1)' },
]) {
  test('start rejects an incompatible or unsafe browser authorization response', async (context) => {
    // Given
    ownSession(context)
    context.mock.method(globalThis, 'fetch', async () => Response.json(response))
    // When / Then
    await assert.rejects(startChatGPTSubscription(), (error) => error instanceof ChatGPTSubscriptionError && error.status === null)
  })
}

test('complete drops sensitive transport error messages', async (context) => {
  // Given
  ownSession(context)
  context.mock.method(globalThis, 'fetch', async () => { throw new TypeError(callback) })
  // When / Then
  await assert.rejects(completeChatGPTSubscription({ attempt_id: 'attempt-one', callback_url: callback }), (error) => {
    assert.ok(error instanceof ChatGPTSubscriptionError)
    assert.equal(error.status, null)
    assert.equal(String(error).includes(callback), false)
    return true
  })
})

test('status forwards caller cancellation through the owned transport', async (context) => {
  // Given
  ownSession(context)
  const controller = new AbortController()
  const response = Promise.withResolvers<Response>()
  const fetch = context.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, _options?: RequestInit) => response.promise)
  const pending = getChatGPTSubscriptionStatus('attempt-one', controller.signal)
  const rejected = assert.rejects(pending, (error) => error instanceof DOMException && error.name === 'AbortError')
  // When
  controller.abort()
  response.resolve(Response.json({ status: 'pending', attempt_id: 'attempt-one' }))
  await rejected
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal?.aborted, true)
})

test('complete rejects a late response body after admin session replacement', async (context) => {
  // Given
  ownSession(context)
  const body = Promise.withResolvers<string>()
  const response = new DeferredBodyResponse(200, body.promise)
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => response)
  const pending = completeChatGPTSubscription({ attempt_id: 'attempt-one', callback_url: callback })
  const rejected = assert.rejects(pending, StaleSessionError)
  await response.bodyStarted.promise
  // When
  adminSession.replace('replacement-chatgpt-token')
  body.resolve(JSON.stringify({ status: 'authorized', attempt_id: 'attempt-one', model_id: 'old-company-model' }))
  await rejected
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal?.aborted, true)
})
