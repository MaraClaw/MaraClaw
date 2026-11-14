import { OrganizationAdmissionQuoteSchema } from './engine-contracts.generated.ts'
import type { OrganizationQuote } from './organization-admissions-api.ts'

/** The quote the landing form received, carried to `/organization-payment` (and across a reload) in this tab only. */
export const ORGANIZATION_QUOTE_KEY = 'maraclaw-organization-quote'

export function storeOrganizationQuote(quote: OrganizationQuote): void {
  sessionStorage.setItem(ORGANIZATION_QUOTE_KEY, JSON.stringify(quote))
}

export function clearOrganizationQuote(): void {
  sessionStorage.removeItem(ORGANIZATION_QUOTE_KEY)
}

function parseQuote(stored: string): OrganizationQuote | null {
  let value: unknown
  try {
    value = JSON.parse(stored)
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error
    return null
  }
  return OrganizationAdmissionQuoteSchema.safeParse(value).data ?? null
}

/** The stored quote, or `null` when none is stored; a malformed or expired one is dropped. */
export function readOrganizationQuote(now = Date.now()): OrganizationQuote | null {
  const stored = sessionStorage.getItem(ORGANIZATION_QUOTE_KEY)
  if (stored === null) return null
  const quote = parseQuote(stored)
  if (quote !== null && Date.parse(quote.expires_at) > now) return quote
  clearOrganizationQuote()
  return null
}
