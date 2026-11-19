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

export type FrameScheduler = {
  readonly request: (callback: () => void) => number
  readonly cancel: (handle: number) => void
}

// Resolves the globals per call so an installed or mocked frame clock is honored.
export const animationFrames: FrameScheduler = {
  request: (callback) => requestAnimationFrame(() => callback()),
  cancel: (handle) => cancelAnimationFrame(handle),
}

/**
 * Chunks are accepted synchronously into `latest()`, so sequence and turn checks never wait for a paint.
 * Subscribers only see them through `getSnapshot()`, which advances at most once per frame.
 * A terminal clear or reset publishes at once and invalidates the pending frame.
 */
export function createLiveStreamStore(frames: FrameScheduler = animationFrames) {
  let accepted = createLiveStream()
  let snapshot = accepted
  let pending: { handle: number } | null = null
  const listeners = new Set<() => void>()
  function publish() {
    if (snapshot === accepted) return
    snapshot = accepted
    for (const listener of listeners) listener()
  }
  function cancelFrame() {
    if (!pending) return
    frames.cancel(pending.handle)
    pending = null
  }
  function schedule() {
    if (pending) return
    const frame = { handle: 0 }
    pending = frame
    frame.handle = frames.request(() => {
      // A scheduler that ignores `cancel` must not publish for a frame that was already invalidated.
      if (pending !== frame) return
      pending = null
      publish()
    })
  }
  return {
    getSnapshot: () => snapshot,
    latest: () => accepted,
    hasLive: () => Boolean(snapshot.text),
    subscribe: (listener: () => void) => {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    chunk: (chunk: ChunkEvent) => {
      const next = applyLiveChunk(accepted, chunk)
      if (next === accepted) return false
      accepted = next
      schedule()
      return true
    },
    clear: (finishedTurnId?: string | null) => {
      const next = clearLiveStream(accepted, finishedTurnId)
      // Finishing another turn keeps the active text; its unpublished chunks still wait for their frame.
      const foreign = next.turnId !== null && next.turnId === accepted.turnId
      accepted = next
      if (foreign && pending) return
      cancelFrame()
      publish()
    },
    reset: () => {
      cancelFrame()
      accepted = createLiveStream()
      publish()
    },
  }
}
