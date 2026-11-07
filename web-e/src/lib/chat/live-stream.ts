import type { ChunkEvent } from '../engine-contracts.generated.ts'

const FINISHED_TURN_LIMIT = 64

export type LiveStream = {
  readonly text: string
  readonly turnId: string | null
  readonly lastSeq: number
  readonly finishedTurnIds: readonly string[]
}

export function createLiveStream(): LiveStream {
  return { text: '', turnId: null, lastSeq: -1, finishedTurnIds: [] }
}

export function applyLiveChunk(state: LiveStream, chunk: ChunkEvent): LiveStream {
  const content = chunk.content ?? ''
  // Uncorrelated servers retain their append-only behavior.
  if (chunk.turn_id == null) return { ...state, text: state.text + content }
  if (state.finishedTurnIds.includes(chunk.turn_id)) return state
  if (state.turnId !== null && state.turnId !== chunk.turn_id) return state

  const current = state.turnId === chunk.turn_id
    ? state
    : { ...state, text: '', turnId: chunk.turn_id, lastSeq: -1 }
  if (chunk.seq != null && chunk.seq <= current.lastSeq) return state

  return {
    ...current,
    text: chunk.replace === true ? content : current.text + content,
    lastSeq: chunk.seq ?? current.lastSeq,
  }
}

export function clearLiveStream(state: LiveStream, finishedTurnId?: string | null): LiveStream {
  const finishedTurnIds = finishedTurnId == null || state.finishedTurnIds.includes(finishedTurnId)
    ? state.finishedTurnIds
    : [...state.finishedTurnIds, finishedTurnId].slice(-FINISHED_TURN_LIMIT)
  if (finishedTurnId != null && state.turnId !== null && finishedTurnId !== state.turnId) {
    return { ...state, finishedTurnIds }
  }
  return { text: '', turnId: null, lastSeq: -1, finishedTurnIds }
}

export type LiveStreamStore = ReturnType<typeof createLiveStreamStore>

export function createLiveStreamStore() {
  let snapshot = createLiveStream()
  const listeners = new Set<() => void>()
  function publish(next: LiveStream) {
    if (snapshot === next) return
    snapshot = next
    for (const listener of listeners) listener()
  }
  return {
    getSnapshot: () => snapshot,
    hasLive: () => Boolean(snapshot.text),
    subscribe: (listener: () => void) => {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    chunk: (chunk: ChunkEvent) => {
      const previous = snapshot
      publish(applyLiveChunk(previous, chunk))
      return snapshot !== previous
    },
    clear: (finishedTurnId?: string | null) => publish(clearLiveStream(snapshot, finishedTurnId)),
    reset: () => publish(createLiveStream()),
  }
}
