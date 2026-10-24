import { useQuery } from '@tanstack/react-query'
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { chatHistoryKey, chatLatestOptions, loadEarlierHistory } from '@/lib/chat-history-query'
import type { ChatHistory } from '@/lib/chat/history'
import { useWorkspaceSession } from './session-workspace'

export function useChatHistory(agentId: string, sessionId: string | undefined, enabled: boolean) {
  const scope = useWorkspaceSession()
  const target = useMemo(() => ({ agentId, sessionId: sessionId ?? '' }), [agentId, sessionId])
  const latest = useQuery({ ...chatLatestOptions(scope, target), enabled: enabled && Boolean(sessionId) }, scope.client)
  const retained = useQuery({
    queryKey: chatHistoryKey(target),
    queryFn: (): ChatHistory => { throw new Error('History is populated by scoped page requests') },
    enabled: false,
    staleTime: Infinity,
  }, scope.client)
  const [earlier, setEarlier] = useState<{ readonly target: typeof target; readonly loading: boolean; readonly error: unknown } | null>(null)
  const requestRef = useRef<AbortController | null>(null)
  const viewRef = useRef(target)
  useLayoutEffect(() => {
    viewRef.current = target
    return () => { requestRef.current?.abort(); requestRef.current = null }
  }, [target])

  const loadEarlier = useCallback(async () => {
    if (!enabled || !sessionId || requestRef.current || !scope.isCurrent()) return
    const controller = new AbortController()
    requestRef.current = controller
    setEarlier({ target, loading: true, error: null })
    try {
      await loadEarlierHistory(scope, target, controller.signal)
      if (!controller.signal.aborted && viewRef.current === target) setEarlier({ target, loading: false, error: null })
    } catch (error) {
      if (!controller.signal.aborted && viewRef.current === target && scope.isCurrent()) {
        setEarlier({ target, loading: false, error })
      }
    } finally {
      if (requestRef.current === controller) requestRef.current = null
    }
  }, [enabled, sessionId, scope, target])

  return {
    data: retained.data?.items,
    isLoading: retained.data === undefined && latest.isPending,
    isError: retained.data === undefined && latest.isError,
    error: latest.error,
    refetch: latest.refetch,
    hasEarlier: retained.data?.has_more ?? false,
    loadingEarlier: earlier?.target === target && earlier.loading,
    earlierError: earlier?.target === target ? earlier.error : null,
    loadEarlier,
  }
}
