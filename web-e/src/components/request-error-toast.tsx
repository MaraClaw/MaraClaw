import { useEffect, useState } from 'react'
import { authSession } from '@/lib/auth-session'
import { showRequestError } from '@/lib/request-error-toast'

/** Mount at the existing error branch, leaving its retry and access controls in place. */
export function RequestErrorToast({ message, error }: { readonly message: string; readonly error?: unknown }) {
  const [owner] = useState(() => authSession.capture())
  useEffect(() => {
    if (!authSession.isCurrent(owner)) return
    showRequestError(message, error)
  }, [message, error, owner])
  return null
}
