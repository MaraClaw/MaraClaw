import assert from 'node:assert/strict'
import { test } from 'node:test'
import { applyLiveChunk, clearLiveStream, createLiveStream } from '../src/lib/chat/live-stream.ts'
import { ChunkEventSchema, DoneEventSchema } from '../src/lib/engine-contracts.generated.ts'

const turnA = '11111111-1111-4111-8111-111111111111'
const turnB = '22222222-2222-4222-8222-222222222222'

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
