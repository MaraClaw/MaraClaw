import { useEffect, useState } from 'react'
import { authSession } from '@/lib/auth-session'
import { showRequestError } from '@/lib/request-error-toast'
import { useTranslation } from '@/hooks/use-translation'
import { ApiError, formatApiDetail } from '@/lib/http'

/** Mount at the existing error branch, leaving its retry and access controls in place. */
export function RequestErrorToast({ message, error }: { readonly message: string; readonly error?: unknown }) {
  const { owned } = useTranslation()
  const [owner] = useState(() => authSession.capture())
  useEffect(() => {
    if (!authSession.isCurrent(owner)) return
    showRequestError(error instanceof ApiError && formatApiDetail(error.detail) === message ? message : owned(message), error)
  }, [message, error, owner, owned])
  return null
}
