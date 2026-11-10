import { toast } from 'sonner'

import { ApiError, formatApiDetail } from './http.ts'
import { StaleSessionError } from './session-policy.ts'

const presented = new WeakSet<object>()

export function showErrorToast(message: string, error?: unknown) {
  if (error instanceof StaleSessionError || (error instanceof Error && ['AbortError', 'CancelledError'].includes(error.name))) return
  if (error instanceof Error) {
    if (presented.has(error)) return
    presented.add(error)
  }
  const text = formatApiDetail(message) ?? (error instanceof ApiError ? error.message : 'The request could not be completed. Please try again.')
  toast.error(text, { id: `api-error:${text}` })
}
