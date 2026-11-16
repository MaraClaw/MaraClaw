import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'
import { ApiError, BinaryLimitError, MAX_PREVIEW_BYTES } from '../src/lib/http.ts'
import { attachmentRenderer } from '../src/lib/attachment-preview-api.ts'
import { attachmentFixture, metadata, resource } from './attachment-fixture.ts'

for (const [content_type, renderer] of [
  ['video/mp4', 'video'], ['video/webm', 'video'], ['video/ogg', 'video'],
  [' VIDEO/MP4 ; codecs="avc1.42E01E"', 'video'],
  ['video/quicktime', 'unsupported'], ['video/x-msvideo', 'unsupported'],
  ['application/octet-stream', 'unsupported'],
] as const) {
  test(`${content_type} selects ${renderer} without trusting a video filename`, () => {
    assert.equal(attachmentRenderer({ ...metadata, name: 'clip.mp4', content_type }), renderer)
  })
}

for (const extension of ['docx', 'xlsx', 'pptx', 'doc', 'xls', 'ppt']) {
  test(`${extension} preview uses authenticated bytes only when its local renderer is supported`, async (context) => {
    // Given
    let byteCalls = 0
    const fixture = attachmentFixture((url) => {
      if (String(url).includes('preview-metadata')) return Promise.resolve(Response.json({ ...metadata, name: `report.${extension}`, content_type: 'application/octet-stream' }))
      byteCalls++
      return Promise.resolve(new Response(new Uint8Array([80, 75])))
    })
    context.after(fixture.dispose)
    // When
    const preview = await fixture.previews.open(fixture.scope, resource, new AbortController().signal)
    // Then
    const supported = ['docx', 'xlsx', 'pptx'].includes(extension)
    assert.equal(preview.renderer, supported ? 'office' : 'unsupported')
    assert.equal(byteCalls, supported ? 1 : 0)
  })
}

for (const content_type of ['image/png', 'video/webm']) {
  test(`${content_type}: each opening reauthorizes metadata before reusing versioned bytes`, async (context) => {
    // Given
    let metadataCalls = 0
    let byteCalls = 0
    const fixture = attachmentFixture((url, options) => {
      assert.equal(options?.cache, 'no-store')
      if (String(url).includes('preview-metadata')) {
        metadataCalls++
        return Promise.resolve(Response.json({ ...metadata, content_type }))
      }
      byteCalls++
      return Promise.resolve(new Response(new Uint8Array([1, 2, 3])))
    })
    context.after(fixture.dispose)
    const first = await fixture.previews.open(fixture.scope, resource, new AbortController().signal)
    // When
    const second = await fixture.previews.open(fixture.scope, resource, new AbortController().signal)
    // Then
    assert.equal(metadataCalls, 2)
    assert.equal(byteCalls, 1)
    assert.ok(first.renderer !== 'unsupported' && second.renderer !== 'unsupported')
    assert.equal(first.bytes, second.bytes)
    assert.ok(fixture.queryClient.getQueryCache().getAll().every((query) => !(query.state.data instanceof Uint8Array)))
  })
}

for (const limit of ['metadata', 'stream'] as const) {
  test(`video ${limit} above 20 MiB is rejected without caching bytes`, async (context) => {
    let byteCalls = 0
    const fixture = attachmentFixture((url, options) => {
      assert.equal(new Headers(options?.headers).get('Authorization'), 'Bearer token-a')
      if (String(url).includes('preview-metadata')) return Promise.resolve(Response.json({
        ...metadata, name: 'clip.webm', content_type: 'video/webm', size: limit === 'metadata' ? MAX_PREVIEW_BYTES + 1 : 3,
      }))
      byteCalls++
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) { controller.enqueue(new Uint8Array(MAX_PREVIEW_BYTES + 1)); controller.close() },
      })))
    })
    context.after(fixture.dispose)
    await assert.rejects(fixture.previews.open(fixture.scope, resource, new AbortController().signal), BinaryLimitError)
    assert.equal(byteCalls, limit === 'metadata' ? 0 : 1)
    assert.equal(fixture.cache.size, 0)
  })
}

test('concurrent opens deduplicate metadata and bytes without sharing view cancellation', async (context) => {
  // Given
  const body = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  let byteCalls = 0
  const fixture = attachmentFixture((url) => {
    if (String(url).includes('preview-metadata')) return Promise.resolve(Response.json(metadata))
    byteCalls++
    started.resolve()
    return body.promise
  })
  context.after(fixture.dispose)
  const controller = new AbortController()
  const first = fixture.previews.open(fixture.scope, resource, controller.signal)
  const rejected = assert.rejects(first, { name: 'AbortError' })
  const second = fixture.previews.open(fixture.scope, resource, new AbortController().signal)
  await started.promise
  // When
  controller.abort()
  body.resolve(new Response(new Uint8Array([4, 5, 6])))
  // Then
  await rejected
  const result = await second
  assert.equal(byteCalls, 1)
  assert.ok(result.renderer === 'image')
  assert.deepEqual([...result.bytes], [4, 5, 6])
})

test('changed metadata fetches the new version rather than displaying cached bytes', async (context) => {
  // Given
  let version = 'v1'
  const versions: string[] = []
  const fixture = attachmentFixture((url) => {
    if (String(url).includes('preview-metadata')) return Promise.resolve(Response.json({ ...metadata, version_token: version }))
    versions.push(new URL(String(url)).searchParams.get('version_token') ?? '')
    return Promise.resolve(new Response(new Uint8Array([version === 'v1' ? 1 : 2])))
  })
  context.after(fixture.dispose)
  await fixture.previews.open(fixture.scope, resource, new AbortController().signal)
  version = 'v2'
  // When
  const result = await fixture.previews.open(fixture.scope, resource, new AbortController().signal)
  // Then
  assert.deepEqual(versions, ['v1', 'v2'])
  assert.ok(result.renderer === 'image')
  assert.deepEqual([...result.bytes], [2])
})

for (const status of [403, 404, 409]) {
  test(`metadata denial ${status} evicts cached bytes instead of displaying them`, async (context) => {
    // Given
    let denied = false
    const fixture = attachmentFixture((url) => Promise.resolve(String(url).includes('preview-metadata')
      ? denied ? Response.json({ detail: 'unavailable' }, { status }) : Response.json(metadata)
      : new Response(new Uint8Array([1]))))
    context.after(fixture.dispose)
    await fixture.previews.open(fixture.scope, resource, new AbortController().signal)
    denied = true
    // When / Then
    await assert.rejects(fixture.previews.open(fixture.scope, resource, new AbortController().signal),
      (error: unknown) => error instanceof ApiError && error.status === status)
    assert.equal(fixture.cache.size, 0)
  })
}

test('malformed metadata rejects before fetching preview bytes or populating the cache', async (context) => {
  // Given
  let byteCalls = 0
  const fixture = attachmentFixture((url) => {
    if (String(url).includes('preview-metadata')) return Promise.resolve(Response.json({ ...metadata, size: '3' }))
    byteCalls++
    return Promise.resolve(new Response(new Uint8Array([1, 2, 3])))
  })
  context.after(fixture.dispose)
  // When
  const result = fixture.previews.open(fixture.scope, resource, new AbortController().signal)
  // Then
  await assert.rejects(result, ZodError)
  assert.equal(byteCalls, 0)
  assert.equal(fixture.cache.size, 0)
})

test('closing the last preview aborts transport and prevents late bytes from being cached', async (context) => {
  // Given
  const started = Promise.withResolvers<void>()
  const body = Promise.withResolvers<Response>()
  let signal: AbortSignal | null | undefined
  const fixture = attachmentFixture((url, options) => {
    if (String(url).includes('preview-metadata')) return Promise.resolve(Response.json(metadata))
    signal = options?.signal
    started.resolve()
    return body.promise
  })
  context.after(fixture.dispose)
  const controller = new AbortController()
  const result = fixture.previews.open(fixture.scope, resource, controller.signal)
  const rejected = assert.rejects(result, { name: 'AbortError' })
  await started.promise
  // When
  controller.abort()
  body.resolve(new Response(new Uint8Array([1])))
  // Then
  await rejected
  assert.equal(signal?.aborted, true)
  assert.equal(fixture.cache.size, 0)
})
