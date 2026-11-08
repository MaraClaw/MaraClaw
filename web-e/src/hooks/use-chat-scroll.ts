import { useCallback, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import type { ChatLine } from '@/lib/chat/transcript'
import { anchoredScrollTop, firstVisibleRow, transcriptEntryId, transcriptLayout, transcriptWindow, type TranscriptAnchor } from '@/lib/chat/transcript-window'

type ChatScrollState = {
  readonly scope: string
  readonly ready: boolean
  readonly hasEarlier: boolean
  readonly loadingEarlier: boolean
  readonly earlierError: unknown
  readonly loadEarlier: () => Promise<void>
  readonly retainLatest: (protectedEntries: ReadonlySet<string>) => void
}
type ScrollSnapshot = { readonly anchor: TranscriptAnchor | undefined; readonly scrollTop: number; readonly stick: boolean }

export function useChatScroll(threadRef: RefObject<HTMLDivElement | null>, state: ChatScrollState, lines: readonly ChatLine[]) {
  const listRef = useRef<HTMLDivElement | null>(null)
  const elements = useRef(new Map<string, HTMLElement>())
  const observerRef = useRef<ResizeObserver | null>(null)
  const [heights, setHeights] = useState<ReadonlyMap<string, number>>(() => new Map())
  const [viewport, setViewport] = useState<{ readonly top: number; readonly height: number; readonly anchor: TranscriptAnchor | undefined; readonly stick: boolean }>({ top: 0, height: 0, anchor: undefined, stick: true })
  const [focused, setFocused] = useState<string | null>(null)
  const layout = useMemo(() => transcriptLayout(lines, heights), [lines, heights])
  const savedRef = useRef<ScrollSnapshot | null>(null)
  const initializedRef = useRef(false)
  const topArmedRef = useRef(true)
  const scopeRef = useRef(state.scope)

  const origin = useCallback(() => {
    const thread = threadRef.current
    const list = listRef.current
    return thread && list ? list.getBoundingClientRect().top - thread.getBoundingClientRect().top + thread.scrollTop : 0
  }, [threadRef])

  const capture = useCallback((preserve = false) => {
    const thread = threadRef.current
    if (!thread) return
    const start = origin()
    const previous = savedRef.current
    let anchor = preserve && previous?.anchor && layout.byId.has(previous.anchor.id) ? previous.anchor : undefined
    if (!anchor) {
      const { top, bottom } = thread.getBoundingClientRect()
      for (const [id, element] of elements.current) {
        const rect = (element.firstElementChild ?? element).getBoundingClientRect()
        if (rect.bottom > top && rect.top < bottom && (!anchor || rect.top - top < anchor.offset)) anchor = { id, offset: rect.top - top }
      }
      const row = layout.rows[firstVisibleRow(layout.rows, thread.scrollTop - start)]
      if (!anchor && row) anchor = { id: row.id, offset: start + row.top - thread.scrollTop }
    }
    savedRef.current = {
      anchor, scrollTop: thread.scrollTop,
      stick: preserve && previous ? previous.stick : thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80,
    }
    const saved = savedRef.current
    setViewport((previous) => previous.top === thread.scrollTop - start && previous.height === thread.clientHeight
      && previous.anchor?.id === saved.anchor?.id && previous.anchor?.offset === saved.anchor?.offset && previous.stick === saved.stick
      ? previous : { top: thread.scrollTop - start, height: thread.clientHeight, anchor: saved.anchor, stick: saved.stick })
  }, [layout, origin, threadRef])

  const updateScroll = useCallback(() => {
    const thread = threadRef.current
    if (!thread) return
    const saved = savedRef.current
    if (state.ready && !initializedRef.current) {
      thread.scrollTop = thread.scrollHeight
      initializedRef.current = true
    } else if (saved?.stick) {
      thread.scrollTop = thread.scrollHeight
    } else if (saved) {
      const element = saved.anchor ? elements.current.get(saved.anchor.id) : undefined
      const next = element && saved.anchor
        ? thread.scrollTop + element.getBoundingClientRect().top - thread.getBoundingClientRect().top - saved.anchor.offset
        : saved.anchor ? anchoredScrollTop(saved.anchor, layout, origin()) : undefined
      thread.scrollTop = next ?? saved.scrollTop
    }
    capture(true)
  }, [state.ready, layout, origin, threadRef, capture])

  useLayoutEffect(() => {
    if (scopeRef.current === state.scope) return
    scopeRef.current = state.scope
    initializedRef.current = false
    savedRef.current = null
    topArmedRef.current = true
    setHeights(new Map())
    setFocused(null)
  }, [state.scope])
  useLayoutEffect(updateScroll, [updateScroll])

  useLayoutEffect(() => {
    const observer = new ResizeObserver((entries) => {
      setHeights((previous) => {
        const ids = layout.byId
        const next = new Map([...previous].filter(([id]) => ids.has(id)))
        let changed = next.size !== previous.size
        for (const entry of entries) {
          const id = entry.target.getAttribute('data-transcript-row')
          const height = entry.borderBoxSize[0]?.blockSize ?? entry.target.getBoundingClientRect().height
          if (id && height > 0 && next.get(id) !== height) { next.set(id, height); changed = true }
        }
        return changed ? next : previous
      })
    })
    observerRef.current = observer
    for (const element of elements.current.values()) observer.observe(element)
    const thread = threadRef.current
    const resize = new ResizeObserver(() => { updateScroll() })
    if (thread) resize.observe(thread)
    return () => { observer.disconnect(); resize.disconnect(); observerRef.current = null }
  }, [threadRef, updateScroll, layout.byId])

  const registerRow = useCallback((id: string, element: HTMLElement | null) => {
    const previous = elements.current.get(id)
    if (previous) observerRef.current?.unobserve(previous)
    if (element) { elements.current.set(id, element); observerRef.current?.observe(element) }
    else elements.current.delete(id)
  }, [])
  const registerList = useCallback((element: HTMLDivElement | null) => { listRef.current = element }, [])

  function loadEarlier() {
    capture()
    const saved = savedRef.current
    if (saved) savedRef.current = { ...saved, stick: false }
    setViewport((previous) => ({ ...previous, stick: false }))
    topArmedRef.current = false
    void state.loadEarlier()
  }

  const pinned = useMemo(() => new Set([
    ...lines.filter((line) => line.pending || line.delivery === 'rejected').map(transcriptEntryId), ...(focused ? [focused] : []),
  ]), [lines, focused])
  const anchoredTop = viewport.anchor && !viewport.stick ? anchoredScrollTop(viewport.anchor, layout, 0) : undefined
  const rows = transcriptWindow(layout, { top: anchoredTop ?? viewport.top, height: viewport.height }, pinned)

  useLayoutEffect(() => {
    if (lines.length > 500 && savedRef.current?.stick && initializedRef.current && !state.loadingEarlier) {
      state.retainLatest(new Set(focused ? [focused] : []))
    }
  }, [lines, focused, state])

  function onScroll() {
    const thread = threadRef.current
    if (!thread) return
    if (savedRef.current?.scrollTop !== thread.scrollTop) capture()
    if (savedRef.current?.stick && !state.loadingEarlier) state.retainLatest(new Set(focused ? [focused] : []))
    if (thread.scrollTop > 160) topArmedRef.current = true
    if (thread.scrollTop <= 160 && topArmedRef.current && initializedRef.current
      && state.hasEarlier && !state.loadingEarlier && !state.earlierError) loadEarlier()
  }

  return { onScroll, loadEarlier, updateScroll, registerList, registerRow, rows, height: layout.height, setFocused }
}

export type ChatTranscriptViewport = Pick<ReturnType<typeof useChatScroll>, 'registerList' | 'registerRow' | 'rows' | 'height' | 'setFocused'>
