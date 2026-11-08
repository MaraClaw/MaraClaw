import { useQueryClient } from '@tanstack/react-query'
import { useContext, useLayoutEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useSessionCurrent } from '@/hooks/use-auth'
import { ToolsEditorPending } from './tools-editor-pending'

/** View-local work: credentials never become mutation variables or cached options. */
export function useToolsSecretAction<Result = unknown>({ success, failure, onAccepted, invalidate = true, invalidateOnError = false }: {
  readonly success: string; readonly failure: string | ((error: Error) => string); readonly onAccepted: (result: Result) => void
  readonly invalidate?: boolean; readonly invalidateOnError?: boolean
}) {
  const queryClient = useQueryClient()
  const setPendingActions = useContext(ToolsEditorPending)
  const sessionCurrent = useSessionCurrent()
  const active = useRef(false)
  const running = useRef(false)
  const [isPending, setPending] = useState(false)
  const [errorMessage, setError] = useState<string | null>(null)
  useLayoutEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const isCurrent = () => active.current && sessionCurrent()
  return {
    isPending, errorMessage,
    submit: async (action: () => Promise<Result>) => {
      if (running.current || !isCurrent()) return
      running.current = true
      setPendingActions?.((count) => count + 1)
      setPending(true)
      setError(null)
      try {
        const result = await action()
        if (!sessionCurrent()) return
        if (invalidate) await queryClient.invalidateQueries({ queryKey: ['admin-tools'] })
        if (!isCurrent()) return
        toast.success(success)
        onAccepted(result)
      } catch (error) {
        if (!(error instanceof Error)) throw error
        if (sessionCurrent() && invalidateOnError) await queryClient.invalidateQueries({ queryKey: ['admin-tools'] })
        if (isCurrent()) setError(typeof failure === 'string' ? failure : failure(error))
      } finally {
        running.current = false
        setPendingActions?.((count) => count - 1)
        if (isCurrent()) setPending(false)
      }
    },
  }
}
