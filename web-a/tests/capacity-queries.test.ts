import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import { MutationObserver, QueryClient, QueryObserver } from '@tanstack/react-query'

import {
  capacityCancelOptions, capacityDecisionOptions, capacityEvidenceOptions, capacityKeys, capacityOrderCreateOptions,
  capacityOrderOptions, capacityOrdersOptions, capacityOverviewOptions, capacityReviewDetailOptions,
  capacityReviewListOptions, capacitySettingsOptions, capacitySettingsSaveOptions,
} from '../src/hooks/capacity-options.ts'
import { notificationKeys } from '../src/hooks/notification-options.ts'
import { ApiError } from '../src/lib/http.ts'
import { capacityOrderId, cappedOverview, purchaseOrder, submittedOrder } from './capacity-fixtures.ts'

function clientFor(t: TestContext) {
  const client = new QueryClient()
  t.after(() => client.clear())
  return client
}

const answer = (body: unknown, status = 200) => async () => Response.json(body, { status })
const invalidated = (client: QueryClient, key: readonly unknown[]) => client.getQueryState(key)?.isInvalidated

test('disabled gates and unusable ids send no capacity reads', (t) => {
  const client = clientFor(t)
  const fetch = t.mock.method(globalThis, 'fetch', answer({}))
  const observers = [
    new QueryObserver(client, capacityOverviewOptions(null, false)),
    new QueryObserver(client, capacityOrdersOptions(null, 10, 0, false)),
    new QueryObserver(client, capacityOrderOptions(capacityOrderId, false)),
    new QueryObserver(client, capacityOrderOptions(null, true)),
    new QueryObserver(client, capacityReviewListOptions('submitted', 0, false)),
    new QueryObserver(client, capacityReviewDetailOptions(capacityOrderId, false)),
    new QueryObserver(client, capacityReviewDetailOptions(null, true)),
    new QueryObserver(client, capacitySettingsOptions(false)),
  ]
  for (const observer of observers) t.after(observer.subscribe(() => undefined))
  assert.equal(fetch.mock.callCount(), 0)
})

test('company, order, queue and settings reads are keyed apart', () => {
  const keys = [
    capacityKeys.overview(null), capacityKeys.overview('tenant-a'), capacityKeys.orders(null, 10, 0), capacityKeys.orders(null, 10, 10),
    capacityKeys.order(capacityOrderId), capacityKeys.reviewList('submitted', 0), capacityKeys.reviewList('approved', 0),
    capacityKeys.reviewDetail(capacityOrderId), capacityKeys.settings,
  ].map((key) => JSON.stringify(key))
  assert.equal(new Set(keys).size, keys.length)
  for (const key of [capacityKeys.overview(null), capacityKeys.reviewList('submitted', 0), capacityKeys.settings]) {
    assert.deepEqual(key.slice(0, 1), capacityKeys.all)
  }
})

test('a denied mutation never sends a request even when called directly', async (t) => {
  const client = clientFor(t)
  const fetch = t.mock.method(globalThis, 'fetch', answer(purchaseOrder))
  const file = new File([new Uint8Array([1])], 'receipt.png', { type: 'image/png' })
  const attempts = [
    new MutationObserver(client, capacityOrderCreateOptions(client, false)).mutate({ extra_accounts: 1, extra_agents: 0 }),
    new MutationObserver(client, capacityEvidenceOptions(client, capacityOrderId, false)).mutate(file),
    new MutationObserver(client, capacityCancelOptions(client, capacityOrderId, false)).mutate(),
    new MutationObserver(client, capacityDecisionOptions(client, capacityOrderId, false)).mutate({ action: 'approve' }),
    new MutationObserver(client, capacitySettingsSaveOptions(client, false)).mutate({ bank_name: 'B', account_number: '1', branch_name: 'C', usd_vnd_rate: 25_000 }),
  ]
  for (const attempt of attempts) await assert.rejects(attempt, (error: unknown) => error instanceof ApiError && error.status === 403)
  assert.equal(fetch.mock.callCount(), 0)
})

test('placing an order stores it and refreshes the overview, lists and notifications', async (t) => {
  const client = clientFor(t)
  client.setQueryData(capacityKeys.overview(null), cappedOverview)
  client.setQueryData(capacityKeys.orders(null, 10, 0), { items: [], has_more: false })
  client.setQueryData(notificationKeys.unread, { unread_count: 0 })
  t.mock.method(globalThis, 'fetch', answer(purchaseOrder, 201))
  const order = await new MutationObserver(client, capacityOrderCreateOptions(client, true)).mutate({ extra_accounts: 2, extra_agents: 3 })
  assert.equal(order.id, capacityOrderId)
  assert.deepEqual(client.getQueryData(capacityKeys.order(capacityOrderId)), purchaseOrder)
  assert.equal(invalidated(client, capacityKeys.overview(null)), true)
  assert.equal(invalidated(client, capacityKeys.orders(null, 10, 0)), true)
  assert.equal(invalidated(client, notificationKeys.unread), true)
})

test('finishing stores the submitted order and refreshes what the reviewers see', async (t) => {
  const client = clientFor(t)
  client.setQueryData(capacityKeys.reviewList('submitted', 0), { items: [], has_more: false })
  t.mock.method(globalThis, 'fetch', answer(submittedOrder))
  const file = new File([new Uint8Array([1])], 'receipt.png', { type: 'image/png' })
  await new MutationObserver(client, capacityEvidenceOptions(client, capacityOrderId, true)).mutate(file)
  assert.deepEqual(client.getQueryData(capacityKeys.order(capacityOrderId)), submittedOrder)
  assert.equal(invalidated(client, capacityKeys.reviewList('submitted', 0)), true)
})

test('cancelling stores the cancelled order', async (t) => {
  const client = clientFor(t)
  t.mock.method(globalThis, 'fetch', answer({ ...purchaseOrder, status: 'cancelled' }))
  await new MutationObserver(client, capacityCancelOptions(client, capacityOrderId, true)).mutate()
  assert.equal(client.getQueryData<{ status: string }>(capacityKeys.order(capacityOrderId))?.status, 'cancelled')
})

test('a decision stores the decided order and refreshes notifications', async (t) => {
  const client = clientFor(t)
  client.setQueryData(notificationKeys.unread, { unread_count: 1 })
  client.setQueryData(capacityKeys.reviewList('submitted', 0), { items: [submittedOrder], has_more: false })
  t.mock.method(globalThis, 'fetch', answer({ ...submittedOrder, status: 'approved' }))
  await new MutationObserver(client, capacityDecisionOptions(client, capacityOrderId, true)).mutate({ action: 'approve' })
  assert.equal(client.getQueryData<{ status: string }>(capacityKeys.reviewDetail(capacityOrderId))?.status, 'approved')
  assert.equal(invalidated(client, capacityKeys.reviewList('submitted', 0)), true)
  assert.equal(invalidated(client, notificationKeys.unread), true)
})

test('saving the settings stores them and refreshes the overview that reports payments_configured', async (t) => {
  const client = clientFor(t)
  client.setQueryData(capacityKeys.overview(null), cappedOverview)
  const saved = { configured: true, bank_name: 'B', account_number: '1', branch_name: 'C', usd_vnd_rate: 25_000 }
  t.mock.method(globalThis, 'fetch', answer(saved))
  await new MutationObserver(client, capacitySettingsSaveOptions(client, true)).mutate({ bank_name: 'B', account_number: '1', branch_name: 'C', usd_vnd_rate: 25_000 })
  assert.deepEqual(client.getQueryData(capacityKeys.settings), saved)
  assert.equal(invalidated(client, capacityKeys.overview(null)), true)
})

test('a 409 refreshes capacity data without touching notifications', async (t) => {
  const client = clientFor(t)
  client.setQueryData(capacityKeys.reviewDetail(capacityOrderId), submittedOrder)
  client.setQueryData(notificationKeys.unread, { unread_count: 1 })
  t.mock.method(globalThis, 'fetch', answer({ detail: { code: 'order_already_decided', message: 'Already decided' } }, 409))
  await assert.rejects(new MutationObserver(client, capacityDecisionOptions(client, capacityOrderId, true)).mutate({ action: 'reject', note: 'No match.' }),
    (error: unknown) => error instanceof ApiError && error.status === 409)
  assert.equal(invalidated(client, capacityKeys.reviewDetail(capacityOrderId)), true)
  assert.equal(invalidated(client, notificationKeys.unread), false)
})

test('a refused purchase refreshes the overview so the form reflects the new state', async (t) => {
  const client = clientFor(t)
  client.setQueryData(capacityKeys.overview(null), cappedOverview)
  t.mock.method(globalThis, 'fetch', answer({ detail: { code: 'purchase_already_open', message: 'Open', order_id: capacityOrderId } }, 409))
  await assert.rejects(new MutationObserver(client, capacityOrderCreateOptions(client, true)).mutate({ extra_accounts: 1, extra_agents: 0 }))
  assert.equal(invalidated(client, capacityKeys.overview(null)), true)
})

test('order writes for one order are serialized in one mutation scope', () => {
  const client = new QueryClient()
  const scopes = [
    capacityEvidenceOptions(client, capacityOrderId, true).scope.id, capacityCancelOptions(client, capacityOrderId, true).scope.id,
    capacityDecisionOptions(client, capacityOrderId, true).scope.id,
  ]
  assert.equal(new Set(scopes).size, 1)
  client.clear()
})
