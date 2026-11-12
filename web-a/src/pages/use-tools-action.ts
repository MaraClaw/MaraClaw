import { useQueryClient } from '@tanstack/react-query'
import { useContext, useLayoutEffect, useRef } from 'react'
import { toast } from 'sonner'

import { useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { useTranslation } from '@/hooks/use-translation'
import type { PlainMessageKey } from '@/lib/translations'
import { ToolsEditorPending } from './tools-editor-pending'

export function useToolsAction<T>(action: (input: T) => Promise<unknown>, {
  success, failure, onDone, onAccepted,
}: {
  readonly success: PlainMessageKey; readonly failure: PlainMessageKey; readonly onDone: () => void
  readonly onAccepted?: () => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const setPendingActions = useContext(ToolsEditorPending)
  const isSessionCurrent = useSessionCurrent()
  const active = useRef(false)
  const running = useRef(false)
  const errorRef = useRef<PlainMessageKey | null>(null)
  useLayoutEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])
  const isCurrent = () => active.current && isSessionCurrent()
  const mutation = useSessionMutation({
    mutationFn: action,
    gcTime: 0,
    onSettled: () => { running.current = false; setPendingActions?.((count) => count - 1) },
    onSuccess: async () => {
      if (!isSessionCurrent()) return
      if (active.current) onAccepted?.()
      await queryClient.invalidateQueries({ queryKey: ['admin-tools'] })
      if (!isCurrent()) return
      toast.success(t(success))
      onDone()
    },
    onError: () => {
      if (isCurrent()) errorRef.current = failure
    },
  })
  return {
    ...mutation,
    submit: (input: T) => {
      if (running.current || !isCurrent()) return
      running.current = true
      setPendingActions?.((count) => count + 1)
      errorRef.current = null
      mutation.mutate(input)
    },
    errorMessage: mutation.isError && isCurrent() && errorRef.current ? t(errorRef.current) : null,
  }
}
