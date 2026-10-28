import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { authSession } from '../src/lib/auth-session.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'
import { chatUploadLimit, formatUploadLimit, getUploadPolicy, type UploadPolicy } from '../src/lib/upload-policy-api.ts'

const policy: UploadPolicy = {
  chat_image_max_bytes: 17, chat_file_max_bytes: 31, workspace_file_max_bytes: 47,
  image_extensions: ['.png', '.jpeg'],
}

test('policy requests validated metadata through the central HTTP boundary', async (context) => {
  // Given
  const signal = new AbortController().signal
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/chat/upload-policy')
    assert.equal(init.method, 'GET')
    assert.equal(init.body, undefined)
    assert.equal(init.signal, signal)
    return Response.json({ ...policy, internal_configuration: 'must not escape' })
  })
  // When
  const actual = await getUploadPolicy({ signal })
  // Then
  assert.deepEqual(actual, policy)
})

for (const malformed of [
  null, [], {}, { ...policy, chat_image_max_bytes: 0 }, { ...policy, chat_file_max_bytes: -1 },
  { ...policy, workspace_file_max_bytes: 1.5 }, { ...policy, chat_image_max_bytes: '17' },
  { ...policy, chat_file_max_bytes: Number.MAX_SAFE_INTEGER + 1 },
  { ...policy, image_extensions: [] }, { ...policy, image_extensions: ['png'] },
  { ...policy, image_extensions: ['.PNG'] }, { ...policy, image_extensions: ['.png', null] },
  { ...policy, image_extensions: ['.png/'] },
]) {
  test(`policy fails closed for malformed metadata ${JSON.stringify(malformed)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When / Then
    await assert.rejects(getUploadPolicy(), ZodError)
  })
}

test('policy preserves HTTP failure rather than substituting fallback limits', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Unavailable' }, { status: 503 }))
  // When / Then
  await assert.rejects(getUploadPolicy(), (error: unknown) => error instanceof ApiError && error.status === 503)
})

for (const status of [200, 401, 503]) {
  test(`policy discards a late ${status} after session replacement`, async (context) => {
    // Given
    const owner = authSession.capture()
    context.mock.method(globalThis, 'fetch', async () => {
      authSession.adoptStoredSession(owner)
      return Response.json(policy, { status })
    })
    // When / Then
    await assert.rejects(getUploadPolicy({ owner }), StaleSessionError)
  })
}

test('policy rejects a body returned after its request was aborted', async (context) => {
  // Given
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async () => { controller.abort(); return Response.json(policy) })
  // When / Then
  await assert.rejects(getUploadPolicy({ signal: controller.signal }), { name: 'AbortError' })
})

for (const [filename, limit] of [
  ['photo.PNG', 17], ['photo.JpEg', 17], ['.photo.PNG', 17], ['..photo.PNG', 17],
  ['.png', 31], ['..PNG', 31], ['...', 31], ['no-extension', 31], ['', 31],
  ['photo.png.txt', 31], ['photo.png.', 31], ['folder/.png', 31],
  ['photo.png/', 17], ['foo//photo.PNG', 17], ['//foo///photo.PNG', 17], ['///', 31],
  ['back\\photo.PNG', 17], ['photo.SVG', 31], ['photo.png/..', 31],
] as const) {
  test(`chat classifies ${JSON.stringify(filename)} by server suffix rules`, () => {
    // Given / When
    const actual = chatUploadLimit(filename, policy)
    // Then
    assert.equal(actual, limit)
  })
}

test('chat honors the fetched extension set instead of a built-in image list', () => {
  // Given
  const changed = { ...policy, image_extensions: ['.odd'] }
  // When / Then
  assert.equal(chatUploadLimit('photo.PNG', changed), 31)
  assert.equal(chatUploadLimit('photo.ODD', changed), 17)
})

test('limit labels preserve authoritative bytes at small and MiB-sized limits', () => {
  // Given / When / Then
  assert.equal(formatUploadLimit(47), '47 bytes')
  assert.equal(formatUploadLimit(10 * 1024 * 1024), '10 MiB')
  assert.equal(formatUploadLimit(50 * 1024 * 1024), '50 MiB')
})
