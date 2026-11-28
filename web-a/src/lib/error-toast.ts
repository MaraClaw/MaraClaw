import { toast } from 'sonner'

import { ApiError, formatApiDetail, REQUEST_TIMEOUT_MESSAGE, RequestTimeoutError } from './http.ts'
import { message, type LocalMessage } from './i18n.ts'
import { StaleSessionError } from './session-policy.ts'

const presented = new WeakSet<object>()

/** How a failed request reads: a timeout keeps its own translated retry wording; any other failure keeps the caller's. */
export function requestFailureMessage<Fallback extends string | LocalMessage>(error: unknown, fallback: Fallback): Fallback | LocalMessage {
  return error instanceof RequestTimeoutError ? message(REQUEST_TIMEOUT_MESSAGE) : fallback
}

export function showErrorToast(message: string, error?: unknown) {
  if (error instanceof StaleSessionError || (error instanceof Error && ['AbortError', 'CancelledError'].includes(error.name))) return
  if (error instanceof Error) {
    if (presented.has(error)) return
    presented.add(error)
  }
  const text = formatApiDetail(message) ?? (error instanceof ApiError ? error.message : 'The request could not be completed. Please try again.')
  toast.error(text, { id: `api-error:${text}` })
}
