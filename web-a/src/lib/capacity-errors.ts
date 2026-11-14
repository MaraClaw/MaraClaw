import { capacityConflict } from './capacity-api.ts'
import { ApiError } from './http.ts'
import type { PlainMessageKey } from './translations.ts'

/** Raised before any request when the signed-in admin may not do this; `key` is the client's own translated copy. */
export class CapacityGuardError extends ApiError {
  readonly key: PlainMessageKey

  constructor(key: PlainMessageKey) {
    super(403, key, key)
    this.key = key
  }
}

export function capacityGuardKey(error: unknown): PlainMessageKey | null {
  return error instanceof CapacityGuardError ? error.key : null
}

/** What every order route answers for a caller who is not (or no longer) this company's organization admin. */
function orderAccessKey(error: ApiError): PlainMessageKey | null {
  if (error.status === 403) return 'Only an organization admin of this company can do this.'
  if (error.status === 404) return 'This order does not exist, or it belongs to another company.'
  return null
}

/**
 * Client copy for a refused or failed evidence upload (`POST /api/capacity/orders/{id}/evidence`). A 413 can come from
 * the engine (JSON) or from nginx (an HTML page the client cannot show), so it is mapped by status alone. `null` keeps
 * the engine's own message.
 */
export function capacityEvidenceErrorKey(error: unknown): PlainMessageKey | null {
  const guard = capacityGuardKey(error)
  if (guard !== null || !(error instanceof ApiError)) return guard
  switch (error.status) {
    case 409:
      return capacityConflict(error)?.code === 'order_not_awaiting_payment'
        ? 'This order is no longer waiting for payment. Its latest status is being refreshed.' : null
    case 413: return 'This file is too large to upload. Choose a file up to 10 MiB.'
    case 415: return 'Choose a PNG, JPEG, WebP or PDF file.'
    case 503: return 'Could not store the evidence. Try again in a moment.'
    default: return orderAccessKey(error)
  }
}

/** Client copy for a refused cancellation (`POST /api/capacity/orders/{id}/cancel`); `null` keeps the engine's message. */
export function capacityCancelErrorKey(error: unknown): PlainMessageKey | null {
  const guard = capacityGuardKey(error)
  if (guard !== null || !(error instanceof ApiError)) return guard
  if (error.status === 409) {
    return capacityConflict(error)?.code === 'order_not_cancellable'
      ? 'Only an order that is waiting for payment can be cancelled. Its latest status is being refreshed.' : null
  }
  return orderAccessKey(error)
}
