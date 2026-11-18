import assert from 'node:assert/strict'
import { test } from 'node:test'
import { applyLiveChunk, clearLiveStream, createLiveStream, createLiveStreamStore, type FrameScheduler } from '../src/lib/chat/live-stream.ts'
import { ChunkEventSchema, DoneEventSchema } from '../src/lib/engine-contracts.generated.ts'

const turnA = '11111111-1111-4111-8111-111111111111'
const turnB = '22222222-2222-4222-8222-222222222222'

function frameClock(options: { readonly honorsCancel?: boolean } = {}) {
  const queue = new Map<number, () => void>()
  let issued = 0
  const scheduler: FrameScheduler = {
    request: (callback) => {
      queue.set(++issued, callback)
      return issued
    },
    cancel: (handle) => {
      if (options.honorsCancel !== false) queue.delete(handle)
    },
  }
  return {
    scheduler,
    requested: () => issued,
    pending: () => queue.size,
    tick: () => {
      const due = [...queue.values()]
      queue.clear()
      for (const callback of due) callback()
    },
    run: (handle: number) => {
      const callback = queue.get(handle)
      queue.delete(handle)
      callback?.()
    },
  }
}

function watched(options?: { readonly honorsCancel?: boolean }) {
  const clock = frameClock(options)
  const store = createLiveStreamStore(clock.scheduler)
  const published: string[] = []
  const stop = store.subscribe(() => published.push(store.getSnapshot().text))
  return { clock, store, published, stop }
}

test('a burst of chunks is accepted in order and published once on the next frame', (t) => {
  const { clock, store, published, stop } = watched()
  t.after(stop)
  const initial = store.getSnapshot()
  for (const [seq, content] of ['One ', 'two ', 'three'].entries()) {
    assert.equal(store.chunk({ type: 'chunk', turn_id: turnA, seq, content }), true)
  }
  assert.equal(clock.requested(), 1)
  assert.equal(store.getSnapshot(), initial)
  assert.equal(store.hasLive(), false)
  assert.equal(store.latest().text, 'One two three')
  assert.deepEqual(published, [])

  clock.tick()

  assert.deepEqual(published, ['One two three'])
  assert.equal(store.hasLive(), true)
  assert.equal(store.getSnapshot(), store.latest())
  clock.tick()
  assert.equal(published.length, 1)
})

test('snapshots stay referentially stable between frames and each frame publishes one new snapshot', (t) => {
  const { clock, store, published, stop } = watched()
  t.after(stop)
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'First' })
  clock.tick()
  const first = store.getSnapshot()
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: ' second' })
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 2, content: ' third' })
  assert.equal(store.getSnapshot(), first)
  assert.equal(first.text, 'First')
  clock.tick()
  assert.notEqual(store.getSnapshot(), first)
  assert.deepEqual(published, ['First', 'First second third'])
  assert.equal(clock.requested(), 2)
})

test('acceptance stays synchronous for stale, duplicate and foreign chunks and schedules no frame for them', (t) => {
  const { clock, store, published, stop } = watched()
  t.after(stop)
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'First' }), true)
  clock.tick()
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Duplicate' }), false)
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnA, seq: -3, content: 'Stale' }), false)
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnB, seq: 1, content: 'Foreign' }), false)
  assert.equal(clock.pending(), 0)
  assert.equal(store.latest().text, 'First')
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: ' next' }), true)
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: ' replay' }), false)
  assert.equal(store.latest().text, 'First next')
  clock.tick()
  assert.deepEqual(published, ['First', 'First next'])
})

test('a replacement swaps the pending text and legacy chunks without a turn keep appending', (t) => {
  const { clock, store, published, stop } = watched()
  t.after(stop)
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Draft ' })
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: 'more' })
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 2, replace: true, content: 'Revised' })
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 3, content: ' text' })
  clock.tick()
  assert.deepEqual(published, ['Revised text'])
  const legacy = watched()
  assert.equal(legacy.store.chunk(ChunkEventSchema.parse({ type: 'chunk', replace: true, content: 'Hello' })), true)
  assert.equal(legacy.store.chunk(ChunkEventSchema.parse({ type: 'chunk', replace: true, content: ' legacy' })), true)
  legacy.clock.tick()
  assert.deepEqual(legacy.published, ['Hello legacy'])
  legacy.stop()
})

test('a terminal clear publishes at once, cancels the pending frame and blocks that turn afterwards', (t) => {
  const { clock, store, published, stop } = watched()
  t.after(stop)
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Visible' })
  clock.tick()
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: ' unpainted' })
  assert.equal(store.latest().text, 'Visible unpainted')

  store.clear(turnA)

  assert.deepEqual(published, ['Visible', ''])
  assert.equal(store.hasLive(), false)
  assert.equal(store.latest().text, '')
  assert.equal(clock.pending(), 0)
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnA, seq: 2, content: ' late' }), false)
  clock.tick()
  assert.deepEqual(published, ['Visible', ''])
})

test('a terminal clear before the first paint leaves nothing to publish', (t) => {
  const { clock, store, published, stop } = watched()
  t.after(stop)
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Never painted' })
  assert.equal(store.latest().text, 'Never painted')

  store.clear(turnA)
  clock.tick()

  assert.equal(store.getSnapshot().text, '')
  assert.equal(published.every((text) => text === ''), true)
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: 'Late' }), false)
})

test('a frame the scheduler failed to cancel cannot repopulate after a clear or a reset', (t) => {
  for (const [name, invalidate] of [['clear', (store: ReturnType<typeof createLiveStreamStore>) => store.clear(turnA)], ['reset', (store: ReturnType<typeof createLiveStreamStore>) => store.reset()]] as const) {
    const { clock, store, published, stop } = watched({ honorsCancel: false })
    t.after(stop)
    store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Old turn' })
    invalidate(store)
    const afterInvalidation = published.length
    assert.equal(clock.pending(), 1, `${name} leaves the cancelled frame queued`)

    clock.tick()

    assert.equal(published.length, afterInvalidation, name)
    assert.equal(store.getSnapshot().text, '', name)
  }
})

test('a stale frame firing ahead of the live one neither publishes early nor lets a second frame be scheduled', (t) => {
  const { clock, store, published, stop } = watched({ honorsCancel: false })
  t.after(stop)
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Old' })
  store.reset()
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'New' })
  assert.equal(clock.requested(), 2)
  const beforeStale = published.length

  clock.run(1)

  assert.equal(published.length, beforeStale)
  assert.equal(store.getSnapshot().text, '')
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: ' more' })
  assert.equal(clock.requested(), 2)
  clock.run(2)
  assert.equal(store.getSnapshot().text, 'New more')
  assert.equal(published.length, beforeStale + 1)
})

test('reset cancels pending work immediately and the next conversation starts on its own frame', (t) => {
  const { clock, store, published, stop } = watched()
  t.after(stop)
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Old conversation' })

  store.reset()

  assert.equal(clock.pending(), 0)
  assert.equal(store.latest().text, '')
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'New conversation' }), true)
  assert.equal(clock.pending(), 1)
  clock.tick()
  assert.equal(store.getSnapshot().text, 'New conversation')
  assert.equal(published.at(-1), 'New conversation')
})

test('finishing another turn keeps the active text and still publishes its unpainted chunks on their frame', (t) => {
  const { clock, store, published, stop } = watched()
  t.after(stop)
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Active' })
  clock.tick()
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: ' still going' })

  store.clear(turnB)

  assert.equal(store.getSnapshot().text, 'Active')
  assert.equal(store.latest().text, 'Active still going')
  assert.equal(store.latest().turnId, turnA)
  assert.equal(clock.pending(), 1)
  assert.equal(store.chunk({ type: 'chunk', turn_id: turnB, seq: 9, content: 'Late foreign' }), false)
  clock.tick()
  assert.deepEqual(published, ['Active', 'Active still going'])
})

test('finishing another turn with nothing pending keeps the published active text', (t) => {
  const { clock, store, stop } = watched()
  t.after(stop)
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Active' })
  clock.tick()

  store.clear(turnB)

  assert.equal(store.getSnapshot().text, 'Active')
  assert.equal(store.getSnapshot().turnId, turnA)
  assert.equal(store.getSnapshot(), store.latest())
  assert.equal(clock.pending(), 0)
})

test('listeners are released on unsubscribe and an unsubscribed listener sees no frame', () => {
  const { clock, store, published, stop } = watched()
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'Heard' })
  clock.tick()
  stop()
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: ' unheard' })
  clock.tick()
  store.clear()
  assert.deepEqual(published, ['Heard'])
  assert.equal(store.getSnapshot().text, '')
})

test('the default scheduler uses requestAnimationFrame and cancels with cancelAnimationFrame', (t) => {
  const frames = new Map<number, FrameRequestCallback>()
  const cancelled: number[] = []
  let issued = 0
  Object.defineProperty(globalThis, 'requestAnimationFrame', { configurable: true, value: (callback: FrameRequestCallback) => { frames.set(++issued, callback); return issued } })
  Object.defineProperty(globalThis, 'cancelAnimationFrame', { configurable: true, value: (handle: number) => { cancelled.push(handle); frames.delete(handle) } })
  t.after(() => {
    Reflect.deleteProperty(globalThis, 'requestAnimationFrame')
    Reflect.deleteProperty(globalThis, 'cancelAnimationFrame')
  })
  const store = createLiveStreamStore()

  store.chunk({ type: 'chunk', turn_id: turnA, seq: 0, content: 'One' })
  store.chunk({ type: 'chunk', turn_id: turnA, seq: 1, content: ' two' })
  assert.equal(frames.size, 1)
  assert.equal(store.getSnapshot().text, '')
  const callback = frames.get(1)
  frames.delete(1)
  callback?.(16)
  assert.equal(store.getSnapshot().text, 'One two')

  store.chunk({ type: 'chunk', turn_id: turnA, seq: 2, content: ' three' })
  store.reset()
  assert.deepEqual(cancelled, [2])
  assert.equal(frames.size, 0)
})

test('increasing correlated chunks append to the live text', () => {
  const state = applyLiveChunk(createLiveStream(), { type: 'chunk', turn_id: turnA, seq: 0, content: 'Hello' })

  const next = applyLiveChunk(state, { type: 'chunk', turn_id: turnA, seq: 1, content: ' world' })

  assert.equal(next.text, 'Hello world')
})

for (const content of ['Revised answer', '', null, undefined]) {
  test(`a replacing chunk sets the whole live text with content ${JSON.stringify(content)}`, () => {
    const state = applyLiveChunk(createLiveStream(), { type: 'chunk', turn_id: turnA, seq: 0, content: 'Draft' })
    const chunk = ChunkEventSchema.parse({ type: 'chunk', turn_id: turnA, seq: 1, replace: true, content })

    const next = applyLiveChunk(state, chunk)

    assert.equal(next.text, content ?? '')
  })
}

for (const seq of [0, 2]) {
  test(`sequence ${seq} cannot overwrite live text after sequence 2`, () => {
    const state = applyLiveChunk(createLiveStream(), { type: 'chunk', turn_id: turnA, seq: 2, content: 'Current' })

    const next = applyLiveChunk(state, { type: 'chunk', turn_id: turnA, seq, replace: true, content: 'Stale' })

    assert.equal(next, state)
  })
}

test('a different turn cannot replace the active stream', () => {
  const state = applyLiveChunk(createLiveStream(), { type: 'chunk', turn_id: turnA, seq: 99, content: 'Old turn' })

  const next = applyLiveChunk(state, { type: 'chunk', turn_id: turnB, seq: 0, content: 'New turn' })

  assert.equal(next, state)
})

test('a new turn binds its first chunk after the active turn completes', () => {
  const state = applyLiveChunk(createLiveStream(), { type: 'chunk', turn_id: turnA, seq: 99, content: 'Old turn' })
  const completed = clearLiveStream(state, turnA)

  const next = applyLiveChunk(completed, { type: 'chunk', turn_id: turnB, seq: 0, content: 'New turn' })

  assert.equal(next.text, 'New turn')
  assert.equal(next.turnId, turnB)
  assert.equal(next.lastSeq, 0)
})

test('a foreign completion preserves active text and blocks its own late chunks', () => {
  const state = applyLiveChunk(createLiveStream(), { type: 'chunk', turn_id: turnA, seq: 2, content: 'Active' })

  const next = clearLiveStream(state, turnB)

  assert.equal(next.text, 'Active')
  assert.equal(next.turnId, turnA)
  assert.equal(next.lastSeq, 2)
  const completed = clearLiveStream(next, turnA)
  assert.equal(applyLiveChunk(completed, { type: 'chunk', turn_id: turnB, seq: 99, content: 'Late foreign' }), completed)
})

for (const turn_id of [undefined, null]) {
  test(`a legacy chunk with turn_id ${turn_id} appends despite replace and sequence metadata`, () => {
    const state = applyLiveChunk(createLiveStream(), { type: 'chunk', turn_id: turnA, seq: 2, content: 'Hello' })
    const chunk = ChunkEventSchema.parse({ type: 'chunk', turn_id, seq: 0, replace: true, content: ' legacy' })

    const next = applyLiveChunk(state, chunk)

    assert.equal(next.text, 'Hello legacy')
    assert.equal(next.lastSeq, 2)
  })
}

for (const seq of [undefined, null]) {
  test(`a correlated chunk with seq ${seq} appends without losing the last known sequence`, () => {
    const state = applyLiveChunk(createLiveStream(), { type: 'chunk', turn_id: turnA, seq: 2, content: 'Hello' })
    const chunk = ChunkEventSchema.parse({ type: 'chunk', turn_id: turnA, seq, replace: null, content: ' again' })

    const next = applyLiveChunk(state, chunk)

    assert.equal(next.text, 'Hello again')
    assert.equal(next.lastSeq, 2)
  })
}

test('done clears live correlation and blocks late chunks for its turn', () => {
  const state = applyLiveChunk(createLiveStream(), { type: 'chunk', turn_id: turnA, seq: 0, content: 'Partial' })
  const done = DoneEventSchema.parse({ type: 'done', turn_id: turnA, content: 'Authoritative answer' })

  const cleared = clearLiveStream(state, done.turn_id)
  const next = applyLiveChunk(cleared, { type: 'chunk', turn_id: turnA, seq: 1, content: 'Late' })

  assert.equal(cleared.text, '')
  assert.equal(cleared.turnId, null)
  assert.equal(cleared.lastSeq, -1)
  assert.equal(next, cleared)
})

test('a finished foreign turn cannot replace the active turn', () => {
  const finished = clearLiveStream(createLiveStream(), turnA)
  const state = applyLiveChunk(finished, { type: 'chunk', turn_id: turnB, seq: 0, content: 'Active' })

  const next = applyLiveChunk(state, { type: 'chunk', turn_id: turnA, seq: 999, replace: true, content: 'Late foreign' })

  assert.equal(next, state)
})

test('clearing a rejected turn resets correlation but retains completed-turn protection', () => {
  const finished = clearLiveStream(createLiveStream(), turnA)
  const state = applyLiveChunk(finished, { type: 'chunk', turn_id: turnB, seq: 99, content: 'Rejected' })

  const cleared = clearLiveStream(state)
  const next = applyLiveChunk(cleared, { type: 'chunk', turn_id: turnB, seq: 0, content: 'Restarted' })

  assert.equal(cleared.text, '')
  assert.equal(cleared.turnId, null)
  assert.equal(next.text, 'Restarted')
  assert.equal(applyLiveChunk(next, { type: 'chunk', turn_id: turnA, seq: 0, content: 'Late' }), next)
})

test('completed-turn protection evicts the oldest ID after 64 distinct completions', () => {
  let state = clearLiveStream(createLiveStream(), turnA)
  for (let index = 0; index < 64; index++) {
    state = clearLiveStream(state, `00000000-0000-4000-8000-${index.toString().padStart(12, '0')}`)
  }

  const next = applyLiveChunk(state, { type: 'chunk', turn_id: turnA, seq: 0, content: 'Evicted' })

  assert.equal(next.text, 'Evicted')
  assert.equal(state.finishedTurnIds.length, 64)
  assert.equal(applyLiveChunk(next, { type: 'chunk', turn_id: '00000000-0000-4000-8000-000000000063', seq: 0, content: 'Late' }), next)
})
