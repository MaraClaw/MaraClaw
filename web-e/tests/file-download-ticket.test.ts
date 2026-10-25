import assert from 'node:assert/strict'
import { test } from 'node:test'

import { authSession } from '../src/lib/auth-session.ts'
import type { AuthSnapshot } from '../src/lib/auth-session.ts'
import { createFilePreview, reduceFilePreview } from '../src/lib/file-download.ts'
import { ApiError } from '../src/lib/http.ts'
import { fileTicketUrl, mintFileDownloadTicket } from '../src/lib/workspace-api.ts'

const jwt = 'session.jwt.must-stay-in-headers'

for (const inline of [false, true]) {
  test(`mint sends a Bearer-authenticated JSON POST with inline=${inline}`, async (context) => {
    // Given
    const snapshot = authSession.getSnapshot()
    context.mock.method(authSession, 'getSnapshot', (): AuthSnapshot => ({
      owner: snapshot.owner, status: 'loading', token: jwt, user: null, mustChangePassword: false,
    }))
    context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
      assert.equal(path, '/api/agents/agent%2Fone/files/download-ticket')
      assert.equal(init.method, 'POST')
      assert.equal(new Headers(init.headers).get('Authorization'), `Bearer ${jwt}`)
      assert.equal(new Headers(init.headers).get('Content-Type'), 'application/json')
      assert.equal(init.body, JSON.stringify({ path: 'workspace/a & b.png', inline }))
      return Response.json({ ticket: 'single-use-ticket', expires_in: 60 })
    })
    // When
    const result = await mintFileDownloadTicket('agent/one', 'workspace/a & b.png', { inline })
    // Then
    assert.deepEqual(result, { ticket: 'single-use-ticket', expires_in: 60 })
  })

  test(`redemption URL is relative, encoded, and ticket-only with inline=${inline}`, () => {
    // Given
    const path = 'workspace/a & b#?+%/image.png'
    // When
    const result = fileTicketUrl('agent/one', path, 'ticket/with+symbols', { inline })
    // Then
    assert.ok(result.startsWith('/api/agents/agent%2Fone/files/download?'))
    const url = new URL(result, 'https://workspace.example')
    assert.equal(url.origin, 'https://workspace.example')
    assert.equal(url.searchParams.get('path'), path)
    assert.equal(url.searchParams.get('inline'), String(inline))
    assert.equal(url.searchParams.get('ticket'), 'ticket/with+symbols')
    assert.ok(result.includes('path=workspace%2Fa+%26+b%23%3F%2B%25%2Fimage.png'))
    assert.ok(!result.includes(jwt))
    assert.ok(!result.includes('token='))
    assert.deepEqual([...url.searchParams.keys()].sort(), ['inline', 'path', 'ticket'])
  })
}

test('mint failure rejects through the shared HTTP client', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Forbidden' }, { status: 403 }))
  // When
  const mint = mintFileDownloadTicket('agent', 'workspace/report.pdf', { inline: false })
  // Then
  await assert.rejects(mint, (error: unknown) => error instanceof ApiError && error.status === 403)
})

test('each download action mints a fresh ticket URL', async (context) => {
  // Given
  let mints = 0
  context.mock.method(globalThis, 'fetch', async () => Response.json({ ticket: `fresh-${++mints}`, expires_in: 60 }))
  // When
  const urls = []
  for (let action = 0; action < 2; action += 1) {
    const { ticket } = await mintFileDownloadTicket('agent', 'workspace/report.pdf', { inline: false })
    urls.push(fileTicketUrl('agent', 'workspace/report.pdf', ticket, { inline: false }))
  }
  // Then
  assert.equal(mints, 2)
  assert.notEqual(urls[0], urls[1])
})

test('an expired or consumed image ticket requests one fresh mint', () => {
  // Given
  const initial = createFilePreview('agent', 'workspace/image.png')
  const ready = reduceFilePreview(initial, { type: 'minted', request: initial.request, url: '/first-ticket' })
  // When
  const retry = reduceFilePreview(ready, { type: 'image-error', request: ready.request })
  // Then
  assert.equal(retry.status, 'minting')
  assert.deepEqual(retry.request, { agentId: 'agent', path: 'workspace/image.png', attempt: 1 })
  assert.notEqual(retry.request, ready.request)
})

test('a second image error gives up rather than looping', () => {
  // Given
  const initial = createFilePreview('agent', 'workspace/image.png')
  const first = reduceFilePreview(initial, { type: 'minted', request: initial.request, url: '/first-ticket' })
  const retry = reduceFilePreview(first, { type: 'image-error', request: first.request })
  const second = reduceFilePreview(retry, { type: 'minted', request: retry.request, url: '/second-ticket' })
  // When
  const failed = reduceFilePreview(second, { type: 'image-error', request: second.request })
  // Then
  assert.equal(failed.status, 'failed')
  assert.equal(reduceFilePreview(failed, { type: 'image-error', request: failed.request }), failed)
})

test('a failed mint shows fallback without an automatic retry', () => {
  // Given
  const initial = createFilePreview('agent', 'workspace/image.png')
  // When
  const failed = reduceFilePreview(initial, { type: 'mint-failed', request: initial.request })
  // Then
  assert.equal(failed.status, 'failed')
  assert.equal(failed.request.attempt, 0)
})

test('a mint result from a previous selection is ignored', () => {
  // Given
  const previous = createFilePreview('agent', 'workspace/old.png')
  const selected = createFilePreview('agent', 'workspace/new.png')
  // When
  const result = reduceFilePreview(selected, { type: 'minted', request: previous.request, url: '/stale-ticket' })
  // Then
  assert.equal(result, selected)
})

test('remounting the same agent and path rejects the previous mount result', () => {
  // Given
  const previous = createFilePreview('agent', 'workspace/image.png')
  const remounted = createFilePreview('agent', 'workspace/image.png')
  // When
  const result = reduceFilePreview(remounted, { type: 'minted', request: previous.request, url: '/used-ticket' })
  // Then
  assert.equal(result, remounted)
  assert.equal(result.request.attempt, 0)
})

test('late failures from the first attempt cannot replace the retry result', () => {
  // Given
  const initial = createFilePreview('agent', 'workspace/image.png')
  const ready = reduceFilePreview(initial, { type: 'minted', request: initial.request, url: '/first-ticket' })
  const retry = reduceFilePreview(ready, { type: 'image-error', request: ready.request })
  // When
  const result = reduceFilePreview(retry, { type: 'mint-failed', request: initial.request })
  // Then
  assert.equal(result, retry)
})
