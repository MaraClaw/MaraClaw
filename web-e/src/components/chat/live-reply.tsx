import { memo, useLayoutEffect, useSyncExternalStore } from 'react'
import type { LiveStreamStore } from '@/lib/chat/live-stream'
import { ChatMarkdown } from './markdown'

export const LiveReply = memo(function LiveReply({ store, onRender }: {
  readonly store: LiveStreamStore
  readonly onRender: () => void
}) {
  const live = useSyncExternalStore(store.subscribe, store.getSnapshot)
  useLayoutEffect(onRender)
  return live.text ? <article className="chat-bubble chat-agent-bubble ml-2 rounded-tl-none text-sm" aria-live="polite" aria-atomic="false">
    <ChatMarkdown text={live.text} />
  </article> : null
})
