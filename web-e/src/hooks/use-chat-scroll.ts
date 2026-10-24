import { useLayoutEffect, useRef, type RefObject } from 'react'

type ScrollSnapshot = {
  readonly anchor: HTMLElement | undefined
  readonly top: number
  readonly scrollTop: number
  readonly stick: boolean
}
type ChatScrollState = {
  readonly scope: string
  readonly ready: boolean
  readonly hasEarlier: boolean
  readonly loadingEarlier: boolean
  readonly earlierError: unknown
  readonly loadEarlier: () => Promise<void>
}

function snapshot(thread: HTMLDivElement): ScrollSnapshot {
  const top = thread.getBoundingClientRect().top
  const anchor = [...thread.querySelectorAll<HTMLElement>('[data-chat-entry]')]
    .find((entry) => entry.getBoundingClientRect().bottom > top)
  return {
    anchor, top: anchor?.getBoundingClientRect().top ?? top, scrollTop: thread.scrollTop,
    stick: thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80,
  }
}

export function useChatScroll(threadRef: RefObject<HTMLDivElement | null>, state: ChatScrollState) {
  const savedRef = useRef<ScrollSnapshot | null>(null)
  const scopeRef = useRef(state.scope)
  const initializedRef = useRef(false)
  const topArmedRef = useRef(true)

  // The saved entry offset describes the DOM before React's next update.
  // Native overflow anchoring is disabled on the thread to avoid double compensation.
  useLayoutEffect(() => {
    const thread = threadRef.current
    if (!thread) return
    if (scopeRef.current !== state.scope) {
      scopeRef.current = state.scope
      initializedRef.current = false
      savedRef.current = null
      topArmedRef.current = true
    }
    const saved = savedRef.current
    if (state.ready && !initializedRef.current) {
      thread.scrollTop = thread.scrollHeight
      initializedRef.current = true
    } else if (saved?.stick) {
      thread.scrollTop = thread.scrollHeight
    } else if (saved) {
      thread.scrollTop = saved.anchor?.isConnected && thread.contains(saved.anchor)
        ? thread.scrollTop + saved.anchor.getBoundingClientRect().top - saved.top
        : saved.scrollTop
    }
    savedRef.current = snapshot(thread)
  })

  function loadEarlier() {
    const thread = threadRef.current
    if (thread) savedRef.current = { ...snapshot(thread), stick: false }
    topArmedRef.current = false
    void state.loadEarlier()
  }

  function onScroll() {
    const thread = threadRef.current
    if (!thread) return
    savedRef.current = snapshot(thread)
    if (thread.scrollTop > 160) topArmedRef.current = true
    if (thread.scrollTop <= 160 && topArmedRef.current && initializedRef.current
      && state.hasEarlier && !state.loadingEarlier && !state.earlierError) {
      loadEarlier()
    }
  }

  return { onScroll, loadEarlier }
}
