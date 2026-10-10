import assert from 'node:assert/strict'
import test from 'node:test'
import { MutationObserver } from '@tanstack/react-query'

import { fenceCallback, ownMutationOptions } from '../src/hooks/session-mutation.ts'
import { createSessionQueryScope, ownQueryClient } from '../src/hooks/session-query-client.ts'
import { StaleSessionError } from '../src/lib/session-policy.ts'
import { createFixture } from './helpers.ts'

for (const outcome of ['success', 'failure'] as const) {
  test(`late mutation ${outcome} suppresses callbacks but does not reverse accepted server writes`, async (t) => {
    // Given
    const { policy } = createFixture()
    const previous = createSessionQueryScope(policy.capture())
    t.after(() => previous.client.clear())
    t.after(ownQueryClient(policy, previous.owner, previous.client))
    const response = Promise.withResolvers<string>()
    const started = Promise.withResolvers<void>()
    const effects: string[] = []
    let acceptedWrites = 0
    const options = ownMutationOptions<string, Error, string, unknown>(policy, previous.owner, {
      mutationFn: () => {
        acceptedWrites++
        started.resolve()
        return response.promise
      },
      onSuccess: () => { effects.push('hook-success') },
      onError: () => { effects.push('hook-error') },
      onSettled: () => { effects.push('hook-settled') },
    })
    const observer = new MutationObserver(previous.client, options)
    t.after(observer.subscribe(() => undefined))
    const isCurrent = () => policy.isCurrent(previous.owner)
    const pending = observer.mutate('company-a', {
      onSuccess: fenceCallback(isCurrent, () => { effects.push('call-success') }),
      onError: fenceCallback(isCurrent, () => { effects.push('call-error') }),
      onSettled: fenceCallback(isCurrent, () => { effects.push('call-settled') }),
    })
    const rejected = assert.rejects(pending, StaleSessionError)
    await started.promise
    // When
    policy.replace('token-b')
    const current = createSessionQueryScope(policy.capture())
    t.after(() => current.client.clear())
    current.client.setQueryData(['admin-companies'], ['company-b'])
    if (outcome === 'success') response.resolve('accepted')
    else response.reject(new TypeError('response lost after acceptance'))
    // Then
    await rejected
    assert.equal(acceptedWrites, 1)
    assert.deepEqual(effects, [])
    assert.equal(previous.client.getMutationCache().getAll().length, 0)
    assert.equal(current.client.getMutationCache().getAll().length, 0)
    assert.deepEqual(current.client.getQueryData(['admin-companies']), ['company-b'])
  })
}

for (const outcome of ['success', 'failure'] as const) {
  test(`current mutation ${outcome} still runs hook and per-call callbacks`, async (t) => {
    // Given
    const { policy } = createFixture()
    const scope = createSessionQueryScope(policy.capture())
    t.after(() => scope.client.clear())
    const effects: string[] = []
    const failure = new TypeError('current write rejected')
    const options = ownMutationOptions<string, Error, string, unknown>(policy, scope.owner, {
      mutationFn: async () => {
        if (outcome === 'failure') throw failure
        return 'accepted'
      },
      onSuccess: () => { effects.push('hook-success') },
      onError: () => { effects.push('hook-error') },
      onSettled: () => { effects.push('hook-settled') },
    })
    const observer = new MutationObserver(scope.client, options)
    t.after(observer.subscribe(() => undefined))
    const isCurrent = () => policy.isCurrent(scope.owner)
    // When
    const pending = observer.mutate('company-a', {
      onSuccess: fenceCallback(isCurrent, () => { effects.push('call-success') }),
      onError: fenceCallback(isCurrent, () => { effects.push('call-error') }),
      onSettled: fenceCallback(isCurrent, () => { effects.push('call-settled') }),
    })
    // Then
    if (outcome === 'failure') await assert.rejects(pending, (error) => error === failure)
    else assert.equal(await pending, 'accepted')
    assert.deepEqual(effects, [`hook-${outcome === 'success' ? 'success' : 'error'}`, 'hook-settled',
      `call-${outcome === 'success' ? 'success' : 'error'}`, 'call-settled'])
  })
}

test('a retired mutation cannot start an optimistic update or server write', async (t) => {
  // Given
  const { policy } = createFixture()
  const scope = createSessionQueryScope(policy.capture())
  t.after(() => scope.client.clear())
  const effects: string[] = []
  const observer = new MutationObserver(scope.client, ownMutationOptions<string, Error, string, unknown>(
    policy, scope.owner, {
      mutationFn: async () => { effects.push('server-write'); return 'accepted' },
      onMutate: () => { effects.push('optimistic-update') },
      onError: () => { effects.push('error-toast') },
    },
  ))
  policy.replace('token-b')
  // When
  const pending = observer.mutate('company-a')
  // Then
  await assert.rejects(pending, StaleSessionError)
  assert.deepEqual(effects, [])
})
