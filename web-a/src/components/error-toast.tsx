import { useEffect } from 'react'

import { useSessionCurrent } from '@/hooks/use-auth'
import { showErrorToast } from '@/lib/error-toast'

export function ErrorToast({ message, error }: {
  readonly message: string
  readonly error?: unknown
}) {
  const isCurrent = useSessionCurrent()
  useEffect(() => {
    if (isCurrent()) showErrorToast(message, error)
  }, [message, error, isCurrent])
  return null
}
