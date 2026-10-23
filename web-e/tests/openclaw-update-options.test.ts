import assert from 'node:assert/strict'
import { test } from 'node:test'
import { MutationObserver } from '@tanstack/react-query'

import { agentUpdateKey, agentUpdateOptions, upgradeOpenClawOptions } from '../src/hooks/openclaw-update-options.ts'
import { fenceMutationOptions, RetiredWorkspaceError } from '../src/hooks/session-query-scope.ts'
import { ApiError } from '../src/lib/http.ts'
import { updateWorkGate } from '../src/lib/openclaw-update-policy.ts'
import { agentId, fixture, notice, run, update } from './openclaw-update-fixture.ts'

test('explicit Upgrade immediately caches an active run and blocks even optional work', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  client.setQueryData(agentUpdateKey(agentId), { ...update, required: false })
  t.mock.method(globalThis, 'fetch', async () => Response.json(run, { status: 202 }))
  const observer = new MutationObserver(client, fenceMutationOptions(scope, upgradeOpenClawOptions(scope, agentId)))
  // When
  await observer.mutate(notice.id)
  // Then
  const status = client.getQueryData< typeof update >(agentUpdateKey(agentId))
  assert.deepEqual(status?.run, run)
  assert.equal(updateWorkGate({ update: status, error: false, submitting: false }), 'upgrading')
  assert.equal(client.getQueryState(agentUpdateKey(agentId))?.isInvalidated, true)
})

for (const state of ['failed', 'succeeded'] as const) {
  test(`terminal ${state} status refreshes related caches and uses authoritative required flag`, async (t) => {
    // Given
    const { client, scope } = fixture()
    t.after(() => client.clear())
    client.setQueryData(agentUpdateKey(agentId), { ...update, run })
    client.setQueryData(['agent', agentId], { status: 'running' })
    client.setQueryData(['agents'], [])
    client.setQueryData(['openclaw-update-detail', notice.id], { notice })
    const terminal = { ...update, required: state === 'failed', run: { ...run, state, failure_code: state === 'failed' ? 'sandbox_unavailable' : null } }
    t.mock.method(globalThis, 'fetch', async () => Response.json(terminal))
    // When
    const result = await client.fetchQuery(agentUpdateOptions(scope, agentId))
    // Then
    assert.equal(updateWorkGate({ update: result, error: false, submitting: false }), state === 'failed' ? 'required' : 'open')
    for (const key of [['agent', agentId], ['agents'], ['openclaw-update-detail', notice.id]]) {
      assert.equal(client.getQueryState(key)?.isInvalidated, true)
    }
    assert.deepEqual(client.getQueryData(agentUpdateKey(agentId)), terminal)
  })
}

test('a failed Upgrade retains the notice and requires an authoritative status refresh', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  client.setQueryData(agentUpdateKey(agentId), update)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail: { code: 'openclaw_update_in_progress' } }, { status: 409 }))
  const observer = new MutationObserver(client, fenceMutationOptions(scope, upgradeOpenClawOptions(scope, agentId)))
  // When / Then
  await assert.rejects(observer.mutate(notice.id), ApiError)
  assert.deepEqual(client.getQueryData(agentUpdateKey(agentId)), update)
  assert.equal(client.getQueryState(agentUpdateKey(agentId))?.isInvalidated, true)
})

test('an older status request cannot overwrite the Upgrade run response', async (t) => {
  // Given
  const { client, scope } = fixture()
  t.after(() => client.clear())
  client.setQueryData(agentUpdateKey(agentId), update)
  const oldResponse = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  let oldSignal: AbortSignal | null | undefined
  t.mock.method(globalThis, 'fetch', (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === 'POST') return Promise.resolve(Response.json(run, { status: 202 }))
    oldSignal = init?.signal
    started.resolve()
    return oldResponse.promise
  })
  const oldPoll = client.fetchQuery(agentUpdateOptions(scope, agentId)).catch((error: unknown) => error)
  await started.promise
  const observer = new MutationObserver(client, fenceMutationOptions(scope, upgradeOpenClawOptions(scope, agentId)))
  // When
  await observer.mutate(notice.id)
  oldResponse.resolve(Response.json({ ...update, required: false }))
  // Then
  await oldPoll
  assert.equal(oldSignal?.aborted, true)
  assert.deepEqual(client.getQueryData(agentUpdateKey(agentId)), { ...update, run })
})

test('a retired session cannot install an Upgrade completion or invalidate a new workspace', async (t) => {
  // Given
  const { client, scope, replace } = fixture()
  t.after(() => client.clear())
  client.setQueryData(agentUpdateKey(agentId), update)
  const response = Promise.withResolvers<Response>()
  const started = Promise.withResolvers<void>()
  t.mock.method(globalThis, 'fetch', () => { started.resolve(); return response.promise })
  const observer = new MutationObserver(client, fenceMutationOptions(scope, upgradeOpenClawOptions(scope, agentId)))
  const rejected = assert.rejects(observer.mutate(notice.id))
  await started.promise
  // When
  replace()
  response.resolve(Response.json(run))
  await rejected
  // Then
  assert.deepEqual(client.getQueryData(agentUpdateKey(agentId)), update)
  assert.equal(client.getQueryState(agentUpdateKey(agentId))?.isInvalidated, false)
})

test('a status poll retired before completion cannot refresh agent caches', async (t) => {
  // Given
  const { client, scope, replace } = fixture()
  t.after(() => client.clear())
  client.setQueryData(['agent', agentId], { status: 'running' })
  const response = Promise.withResolvers<Response>()
  t.mock.method(globalThis, 'fetch', () => response.promise)
  const rejected = assert.rejects(client.fetchQuery(agentUpdateOptions(scope, agentId)))
  // When
  replace()
  response.resolve(Response.json({ ...update, required: false, run: { ...run, state: 'succeeded' } }))
  await rejected
  // Then
  assert.equal(client.getQueryData(agentUpdateKey(agentId)), undefined)
  assert.equal(client.getQueryState(['agent', agentId])?.isInvalidated, false)
  assert.throws(scope.assertCurrent, RetiredWorkspaceError)
})
