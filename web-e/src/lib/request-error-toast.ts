import { toast } from 'sonner'
import { formatApiDetail } from './http.ts'

const reported = new WeakSet<object>()

/** Presentation only: callers retain ownership of domain handling and session fences. */
export function showRequestError(message: string, error?: unknown): void {
  if (error instanceof Error && ['AbortError', 'StaleSessionError', 'RetiredWorkspaceError'].includes(error.name)) return
  if (error !== null && typeof error === 'object') {
    if (reported.has(error)) return
    reported.add(error)
  }
  const safeMessage = formatApiDetail(message) ?? 'Unable to complete the request. Please try again.'
  toast.error(safeMessage, { id: `request-error:${safeMessage}` })
}
