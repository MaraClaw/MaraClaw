import { ApiError } from './http.ts'
import type { TranslationKey } from './i18n/catalog.ts'
import { organizationRefusalCode } from './organization-admissions-api.ts'

export const RECEIPT_MAX_BYTES = 10 * 1024 * 1024
export const RECEIPT_ACCEPT = 'image/png,image/jpeg,image/webp,application/pdf'
const receiptTypes: readonly string[] = ['image/png', 'image/jpeg', 'image/webp', 'application/pdf']
const receiptExtension = /\.(?:png|jpe?g|webp|pdf)$/i

/**
 * Early feedback only: the engine decides the real type from the leading bytes and enforces the same size cap.
 * Returns a translation key, or `null` for a file worth uploading.
 */
export function receiptFileProblem(file: Pick<File, 'name' | 'type' | 'size'>): TranslationKey | null {
  const typeKnown = receiptTypes.includes(file.type)
  const typeUnset = file.type === '' || file.type === 'application/octet-stream'
  if (!typeKnown && !(typeUnset && receiptExtension.test(file.name))) return 'Choose a PNG, JPEG, WebP or PDF file.'
  if (file.size === 0) return 'This file is empty. Choose another file.'
  if (file.size > RECEIPT_MAX_BYTES) return 'This file is larger than 10 MiB. Choose a smaller file.'
  return null
}

/**
 * Client copy for a refused quote or submission. A 413 or 415 can come from the engine (a plain string) or from a proxy
 * in front of it, so both map by status alone. `null` leaves the failure to `userFacingRequestError`.
 */
export function organizationAdmissionErrorKey(error: unknown): TranslationKey | null {
  if (!(error instanceof ApiError)) return null
  switch (error.status) {
    case 409:
      switch (organizationRefusalCode(error)) {
        case 'email_registered': return 'This email address already has an account. Sign in to continue.'
        case 'admission_already_pending': return 'A request for this email address is already waiting for review.'
        case 'domain_claimed': return 'This email domain already belongs to another organization. Use a different email address.'
        case 'payments_not_configured': return 'Organization requests are temporarily unavailable. Try again later.'
        case 'quote_already_used': return 'This price quote was already used for an earlier request. Start again from the pricing section to get a new one.'
        default: return null
      }
    case 413: return 'This file is larger than 10 MiB. Choose a smaller file.'
    case 415: return 'Choose a PNG, JPEG, WebP or PDF file.'
    case 429: return 'Too many requests. Try again later.'
    default: return null
  }
}

/** A malformed (400 `quote_invalid`) or expired (410 `quote_expired`) quote cannot be submitted again. */
export function isOrganizationQuoteLapsed(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 400 || error.status === 410)
}
