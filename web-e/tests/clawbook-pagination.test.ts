import assert from 'node:assert/strict'
import { test } from 'node:test'
import { InfiniteQueryObserver, MutationObserver, QueryClient } from '@tanstack/react-query'
import { z } from 'zod'

import { listClawbookPostPage, type ClawbookComment, type ClawbookPost } from '../src/lib/clawbook-api.ts'
import { clawbookCommentOptions, clawbookPostOptions, clawbookPublishOptions, distinctClawbookItems } from '../src/lib/clawbook-query.ts'

const first: ClawbookPost = {
  id: '11111111-1111-4111-8111-111111111111', author_id: '22222222-2222-4222-8222-222222222222',
  author_type: 'human', author_name: 'Member', content: 'First', likes_count: 0, comments_count: 2,
  created_at: '2030-01-01T00:00:00.123456+07:00',
}
const second: ClawbookPost = { ...first, id: '33333333-3333-4333-8333-333333333333', content: 'Second' }
const published: ClawbookPost = { ...first, id: '44444444-4444-4444-8444-444444444444', content: 'Published' }
const cursor = 'opaque/+?='

test('posts follow opaque keyset cursors, retain first duplicate and stop at the final page', async (t) => {
  // Given
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  t.after(() => client.clear())
  const requests: (string | null)[] = []
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://fixture.test')
    assert.equal(url.pathname, '/api/clawbook/posts/page')
    assert.equal(url.searchParams.get('limit'), '30')
    assert.equal(url.searchParams.has('offset'), false)
    requests.push(url.searchParams.get('cursor'))
    return Response.json(requests.length === 1
      ? { items: [first], next_cursor: cursor }
      : { items: [{ ...first, content: 'Duplicate copy' }, second], next_cursor: null })
  })
  const observer = new InfiniteQueryObserver(client, clawbookPostOptions())
  // When
  await observer.refetch()
  await observer.fetchNextPage()
  // Then
  assert.deepEqual(requests, [null, cursor])
  assert.deepEqual(distinctClawbookItems(observer.getCurrentResult().data?.pages), [first, second])
  assert.equal(observer.getCurrentResult().hasNextPage, false)
})

test('comments use the selected post scope, dedupe ids and accept an empty terminal page', async (t) => {
  // Given
  const comment: ClawbookComment = { ...first, post_id: first.id }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  t.after(() => client.clear())
  const requests: (string | null)[] = []
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://fixture.test')
    assert.equal(url.pathname, `/api/clawbook/posts/${first.id}/comments/page`)
    requests.push(url.searchParams.get('cursor'))
    return Response.json(requests.length === 1 ? { items: [comment], next_cursor: cursor } : { items: [], next_cursor: null })
  })
  const observer = new InfiniteQueryObserver(client, clawbookCommentOptions(first.id))
  // When
  await observer.refetch()
  await observer.fetchNextPage()
  // Then
  assert.deepEqual(requests, [null, cursor])
  assert.deepEqual(distinctClawbookItems(observer.getCurrentResult().data?.pages).map((item) => item.id), [comment.id])
  assert.equal(observer.getCurrentResult().hasNextPage, false)
  assert.notDeepEqual(clawbookCommentOptions(first.id).queryKey, clawbookCommentOptions(second.id).queryKey)
})

test('publishing aborts an in-flight continuation, discards old pages and ignores its late result', async (t) => {
  // Given
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  t.after(() => client.clear())
  const continuation = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<AbortSignal>()
  const requests: (string | null)[] = []
  let didPublish = false
  t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') {
      assert.deepEqual(JSON.parse(String(init.body)), { content: published.content })
      didPublish = true
      return Response.json(published)
    }
    const position = new URL(String(input), 'http://fixture.test').searchParams.get('cursor')
    requests.push(position)
    if (position === 'last') {
      assert.ok(init?.signal)
      started.resolve(init.signal)
      return continuation.promise
    }
    return Response.json(position === cursor
      ? { items: [second], next_cursor: 'last' }
      : { items: [didPublish ? published : first], next_cursor: cursor })
  })
  const observer = new InfiniteQueryObserver(client, clawbookPostOptions())
  t.after(observer.subscribe(() => undefined))
  await observer.refetch()
  await observer.fetchNextPage()
  requests.length = 0
  const inFlight = observer.fetchNextPage()
  const signal = await started.promise
  // When
  await new MutationObserver(client, clawbookPublishOptions(client)).mutate({ content: published.content })
  continuation.resolve(Response.json({ items: [second], next_cursor: null }))
  await inFlight
  // Then
  assert.equal(signal.aborted, true)
  assert.deepEqual(requests, ['last', null])
  assert.deepEqual(observer.getCurrentResult().data, { pages: [{ items: [published], next_cursor: cursor }], pageParams: [null] })
})

for (const malformed of [[], {}, { items: [] }, { items: [null], next_cursor: null }, { items: [], next_cursor: '' }, { items: [], next_cursor: 5 }]) {
  test(`page boundary rejects malformed feed envelope ${JSON.stringify(malformed)}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When / Then
    await assert.rejects(listClawbookPostPage(), z.ZodError)
  })
}
