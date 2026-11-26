import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ApiError } from '../src/lib/http.ts'
import { nextPollDelay, POLL_CEILING_MS, queryPollInterval, type PollEnvironment } from '../src/lib/poll-policy.ts'

const visible = (random: number): PollEnvironment => ({ hidden: () => false, random: () => random })
const hidden: PollEnvironment = { hidden: () => true, random: () => 1 }
const delays = (cadence: number, random: number, failures: readonly number[]) =>
  failures.map((count) => nextPollDelay({ cadence, failures: count, retryAfterMs: null }, visible(random)))

test('a healthy poll keeps its cadence and ignores a stale Retry-After', () => {
  assert.equal(nextPollDelay({ cadence: 1500, failures: 0, retryAfterMs: null }, visible(1)), 1500)
  assert.equal(nextPollDelay({ cadence: 1500, failures: 0, retryAfterMs: 20_000 }, visible(1)), 1500)
})

test('a failure streak doubles the delay until it reaches the ceiling', () => {
  assert.deepEqual(delays(1500, 1, [1, 2, 3, 4, 5, 6, 7, 50]), [3000, 6000, 12_000, 24_000, 48_000, 60_000, 60_000, 60_000])
  assert.equal(POLL_CEILING_MS, 60_000)
})

test('jitter spreads each delay over the upper half of its ceiling without going below the cadence', () => {
  assert.deepEqual(delays(1500, 0, [1, 2, 3, 4, 5, 6, 7]), [1500, 3000, 6000, 12_000, 24_000, 30_000, 30_000])
  assert.deepEqual(delays(1500, 0.5, [1, 2]), [2250, 4500])
  assert.deepEqual(delays(30_000, 0, [1]), [30_000])
  assert.deepEqual(delays(30_000, 1, [1]), [60_000])
  assert.equal(nextPollDelay({ cadence: 90_000, failures: 1, retryAfterMs: null }, visible(0)), 90_000)
})

test('a Retry-After longer than the backoff wins and the ceiling still caps it', () => {
  const after = (retryAfterMs: number) => nextPollDelay({ cadence: 1500, failures: 1, retryAfterMs }, visible(1))
  assert.equal(after(20_000), 20_000)
  assert.equal(after(1000), 3000)
  assert.equal(after(0), 3000)
  assert.equal(after(3_600_000), POLL_CEILING_MS)
})

test('a hidden page is not polled, whatever the failures or Retry-After', () => {
  for (const failures of [0, 1, 9]) assert.equal(nextPollDelay({ cadence: 1500, failures, retryAfterMs: 5000 }, hidden), false)
})

type Probe = { state: { data: string | undefined; error: Error | null; dataUpdateCount: number; errorUpdateCount: number } }

const probe = (): Probe => ({ state: { data: undefined, error: null, dataUpdateCount: 0, errorUpdateCount: 0 } })
const fail = ({ state }: Probe, error: Error) => { state.error = error; state.errorUpdateCount++ }
const succeed = ({ state }: Probe, data: string) => { state.data = data; state.error = null; state.dataUpdateCount++ }

test('a query poll counts consecutive failures from the query state and starts over after a success', () => {
  const poll = queryPollInterval<string>(() => 1500, visible(1))
  const query = probe()
  assert.equal(poll(query), 1500)
  fail(query, new Error('offline'))
  assert.equal(poll(query), 3000)
  fail(query, new Error('offline'))
  assert.equal(poll(query), 6000)
  fail(query, new Error('offline'))
  assert.equal(poll(query), 12_000)
  succeed(query, 'ok')
  assert.equal(poll(query), 1500)
  fail(query, new Error('offline'))
  assert.equal(poll(query), 3000)
})

test('a query poll waits for the Retry-After of the failure that ended its last request', () => {
  const poll = queryPollInterval<string>(() => 1500, visible(1))
  const query = probe()
  poll(query)
  fail(query, new ApiError(429, null, undefined, 20_000))
  assert.equal(poll(query), 20_000)
  fail(query, new ApiError(503, null, undefined, null))
  assert.equal(poll(query), 6000)
  succeed(query, 'ok')
  assert.equal(poll(query), 1500)
})

test('a query poll takes its healthy cadence from the latest data and stays off when the data asks for no polling', () => {
  const poll = queryPollInterval<string>((data) => data === undefined ? false : data === 'busy' ? 1500 : 30_000, visible(1))
  const query = probe()
  assert.equal(poll(query), false)
  succeed(query, 'busy')
  assert.equal(poll(query), 1500)
  succeed(query, 'idle')
  assert.equal(poll(query), 30_000)
  fail(query, new Error('offline'))
  assert.equal(poll(query), 60_000)
})

test('a query poll pauses while the page is hidden and keeps counting failures meanwhile', () => {
  let isHidden = false
  const poll = queryPollInterval<string>(() => 1500, { hidden: () => isHidden, random: () => 1 })
  const query = probe()
  poll(query)
  fail(query, new Error('offline'))
  isHidden = true
  assert.equal(poll(query), false)
  fail(query, new Error('offline'))
  assert.equal(poll(query), false)
  isHidden = false
  assert.equal(poll(query), 6000)
})
