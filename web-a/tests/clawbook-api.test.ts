import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'

import { listClawbookCommentPage, listClawbookPostPage } from '../src/lib/clawbook-api.ts'
import { canViewClawbook } from '../src/lib/types/auth-guards.ts'
import { adminUser } from './helpers.ts'

const post = {
  id: '6f0f3c35-7a8d-4f43-9b1e-2f0b0f6a1c11', author_id: '1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed', author_type: 'agent',
  author_name: 'Ops agent', content: 'Shipped\nthe report', likes_count: 2, comments_count: 1, created_at: '2026-11-18T09:30:00+00:00', hidden_from_members: true,
}
const comment = {
  id: '9a2f8c1e-3b4d-4e5f-8a6b-7c8d9e0f1a2b', post_id: post.id, author_id: '3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f', author_type: 'human',
  author_name: 'Linh', content: 'Thanks', created_at: '2026-11-18T10:00:00+00:00', hidden_from_members: false,
}
const page = (items: readonly unknown[], next_cursor: string | null = null) => ({ items, next_cursor })

test('post pages start without a cursor, forward cancellation and drop unknown fields', async (t) => {
  // Given
  const controller = new AbortController()
  const fetch = t.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ ...page([{ ...post, tenant_id: 'other' }], 'next/+?='), extra: true }))
  // When
  const result = await listClawbookPostPage({ signal: controller.signal })
  controller.abort()
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/clawbook/admin/posts/page?limit=30')
  assert.equal(options?.signal?.aborted, true)
  assert.deepEqual(result, page([post], 'next/+?='))
  assert.equal(Object.isFrozen(result.items[0]), true)
})

test('post pages encode the opaque cursor', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(page([post])))
  // When
  await listClawbookPostPage({ cursor: 'older/+?=' })
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/clawbook/admin/posts/page?limit=30&cursor=older%2F%2B%3F%3D')
})

test('comment pages request the post and encode the opaque cursor', async (t) => {
  // Given
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(page([comment])))
  // When
  const result = await listClawbookCommentPage(post.id, { cursor: 'a&b' })
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], `/api/clawbook/admin/posts/${post.id}/comments/page?limit=30&cursor=a%26b`)
  assert.deepEqual(result, page([comment]))
})

for (const [problem, malformed] of [
  ['an unknown author type', page([{ ...post, author_type: 'user' }])],
  ['a negative count', page([{ ...post, likes_count: -1 }])],
  ['an invalid timestamp', page([{ ...post, created_at: 'yesterday' }])],
  ['more items than the page size', page(Array.from({ length: 31 }, () => post))],
  ['an empty cursor', page([post], '')],
  ['a missing cursor', { items: [post] }],
  ['a missing hidden flag', page([{ ...post, hidden_from_members: undefined }])],
] as const) {
  test(`post pages reject ${problem}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When / Then
    await assert.rejects(listClawbookPostPage(), z.ZodError)
  })
}

for (const [problem, malformed] of [
  ['an unknown author type', page([{ ...comment, author_type: 'bot' }])],
  ['a missing hidden flag', page([{ ...comment, hidden_from_members: undefined }])],
] as const) {
  test(`comment pages reject ${problem}`, async (t) => {
    // Given
    t.mock.method(globalThis, 'fetch', async () => Response.json(malformed))
    // When / Then
    await assert.rejects(listClawbookCommentPage(post.id), z.ZodError)
  })
}

test('only active own-org admins can view Clawbook', () => {
  // Given
  const user = adminUser()
  const denied = [null, { ...user, role: 'member' }, { ...user, role: 'platform_admin' }, { ...user, is_platform_admin: true }, { ...user, tenant_id: null }, { ...user, is_active: false }] as const
  // When / Then
  assert.equal(canViewClawbook(user), true)
  assert.equal(canViewClawbook({ ...user, is_genesis: true }), true)
  assert.ok(denied.every((viewer) => !canViewClawbook(viewer)))
})
