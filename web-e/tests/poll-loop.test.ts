import assert from 'node:assert/strict'
import { test } from 'node:test'

import { ApiError } from '../src/lib/http.ts'
import { startPollLoop, type PollLoopEnvironment } from '../src/lib/poll-loop.ts'
import { drain, manualClock, signal } from './socket-fixture.ts'

function harness() {
  const clock = manualClock()
  const visibility = signal(true)
  const env: PollLoopEnvironment = {
    hidden: () => !visibility.current(), random: () => 1, schedule: clock.schedule, onVisible: visibility.subscribe,
  }
  const ticks: number[] = []
  const outcomes: Array<Error | null> = []
  const tick = async () => {
    ticks.push(clock.now())
    const outcome = outcomes.shift()
    if (outcome) throw outcome
  }
  // Fires due timers one at a time and lets each tick's continuation run before the next, as a browser's task queue would.
  const elapse = async (ms: number) => {
    const end = clock.now() + ms
    for (let due = clock.nextDue(); due !== undefined && due <= end; due = clock.nextDue()) {
      clock.advance(Math.max(0, due - clock.now()))
      await drain()
    }
    clock.advance(end - clock.now())
  }
  return { clock, visibility, env, ticks, outcomes, tick, elapse }
}

test('a healthy loop ticks right away when asked to and then once per cadence', async (t) => {
  // Given
  const { env, ticks, tick, elapse } = harness()
  const stop = startPollLoop({ cadence: 1000, tick, immediate: true }, env)
  t.after(stop)
  // When
  await drain()
  await elapse(3000)
  // Then
  assert.deepEqual(ticks, [0, 1000, 2000, 3000])
})

test('a loop that does not start immediately waits one cadence for its first tick', async (t) => {
  // Given
  const { env, ticks, tick, elapse } = harness()
  t.after(startPollLoop({ cadence: 5000, tick }, env))
  // When
  await elapse(4999)
  assert.deepEqual(ticks, [])
  await elapse(1)
  // Then
  assert.deepEqual(ticks, [5000])
})

test('each failed tick doubles the wait up to the ceiling and a success returns to the cadence', async (t) => {
  // Given
  const { env, ticks, outcomes, tick, elapse } = harness()
  outcomes.push(new Error('down'), new Error('down'), new Error('down'), null)
  t.after(startPollLoop({ cadence: 1000, tick, immediate: true }, env))
  // When
  await drain()
  await elapse(14_000 + 1000)
  // Then
  assert.deepEqual(ticks, [0, 2000, 6000, 14_000, 15_000])
})

test('the wait never exceeds the ceiling however long the streak', async (t) => {
  // Given
  const { env, ticks, outcomes, tick, elapse } = harness()
  for (let failure = 0; failure < 12; failure++) outcomes.push(new Error('down'))
  t.after(startPollLoop({ cadence: 1000, tick, immediate: true }, env))
  // When
  await drain()
  await elapse(1_000_000)
  // Then
  const gaps = ticks.slice(1).map((time, index) => time - (ticks[index] ?? 0))
  assert.deepEqual(gaps.slice(0, 7), [2000, 4000, 8000, 16_000, 32_000, 60_000, 60_000])
  assert.equal(Math.max(...gaps), 60_000)
})

test('a Retry-After on the failed request sets the wait, capped at the ceiling', async (t) => {
  // Given
  const { env, ticks, outcomes, tick, elapse } = harness()
  outcomes.push(new ApiError(429, null, undefined, 20_000), new ApiError(503, null, undefined, 3_600_000))
  t.after(startPollLoop({ cadence: 1000, tick, immediate: true }, env))
  // When
  await drain()
  await elapse(20_000 + 60_000)
  // Then
  assert.deepEqual(ticks, [0, 20_000, 80_000])
})

test('a hidden page lets the pending timer lapse, and becoming visible polls at once', async (t) => {
  // Given
  const { clock, visibility, env, ticks, tick, elapse } = harness()
  t.after(startPollLoop({ cadence: 1000, tick }, env))
  visibility.set(false)
  // When
  await elapse(10_000)
  // Then
  assert.deepEqual(ticks, [])
  assert.equal(clock.pending, 0)
  visibility.set(true)
  await drain()
  assert.deepEqual(ticks, [10_000])
  assert.equal(clock.pending, 1)
  await elapse(1000)
  assert.deepEqual(ticks, [10_000, 11_000])
})

test('a page hidden and shown again before the timer fires keeps its schedule', async (t) => {
  // Given
  const { visibility, env, ticks, tick, elapse } = harness()
  t.after(startPollLoop({ cadence: 1000, tick }, env))
  // When
  await elapse(400)
  visibility.set(false)
  visibility.set(true)
  await drain()
  // Then
  assert.deepEqual(ticks, [])
  await elapse(600)
  assert.deepEqual(ticks, [1000])
})

test('a loop that starts on a hidden page waits for it to become visible', async (t) => {
  // Given
  const { clock, visibility, env, ticks, tick } = harness()
  visibility.set(false)
  t.after(startPollLoop({ cadence: 1000, tick, immediate: true }, env))
  await drain()
  assert.deepEqual(ticks, [])
  assert.equal(clock.pending, 0)
  // When
  visibility.set(true)
  await drain()
  // Then
  assert.deepEqual(ticks, [0])
  assert.equal(clock.pending, 1)
})

test('a visibility change during a slow tick does not start a second request', async (t) => {
  // Given
  const { clock, visibility, env, ticks } = harness()
  const reply = Promise.withResolvers<void>()
  t.after(startPollLoop({ cadence: 1000, immediate: true, tick: async () => { ticks.push(clock.now()); await reply.promise } }, env))
  await drain()
  // When
  visibility.set(false)
  visibility.set(true)
  await drain()
  // Then
  assert.equal(ticks.length, 1)
  reply.resolve()
  await drain()
  assert.equal(clock.pending, 1)
})

test('stopping cancels the timer and the listener, and a tick finishing later schedules nothing', async () => {
  // Given
  const { clock, visibility, env, ticks } = harness()
  const reply = Promise.withResolvers<void>()
  const stop = startPollLoop({ cadence: 1000, immediate: true, tick: async () => { ticks.push(clock.now()); await reply.promise } }, env)
  await drain()
  // When
  stop()
  reply.resolve()
  await drain()
  // Then
  assert.equal(clock.pending, 0)
  assert.equal(visibility.listeners, 0)
  visibility.set(false)
  visibility.set(true)
  await drain()
  assert.equal(ticks.length, 1)
})
