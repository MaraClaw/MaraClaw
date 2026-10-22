import { useQueryClient } from '@tanstack/react-query'
import { useLayoutEffect, useRef } from 'react'
import { toast } from 'sonner'

import { useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'

export function useToolsAction<T>(action: (input: T) => Promise<unknown>, {
  success, failure, onDone, onAccepted,
}: {
  readonly success: string; readonly failure: string; readonly onDone: () => void
  readonly onAccepted?: () => void
}) {
  const queryClient = useQueryClient()
  const isSessionCurrent = useSessionCurrent()
  const active = useRef(false)
  const running = useRef(false)
  const errorRef = useRef<string | null>(null)
  useLayoutEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])
  const isCurrent = () => active.current && isSessionCurrent()
  const mutation = useSessionMutation({
    mutationFn: action,
    gcTime: 0,
    onSettled: () => { running.current = false },
    onSuccess: async () => {
      if (!isSessionCurrent()) return
      if (active.current) onAccepted?.()
      await queryClient.invalidateQueries({ queryKey: ['admin-tools'] })
      if (!isCurrent()) return
      toast.success(success)
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
      errorRef.current = null
      mutation.mutate(input)
    },
    errorMessage: mutation.isError && isCurrent() ? errorRef.current : null,
  }
}
