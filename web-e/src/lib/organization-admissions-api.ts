import { z } from 'zod'
import { getCountries } from 'libphonenumber-js/max'
import { DEFAULT_PHONE_COUNTRY, toE164 } from './phone.ts'

import { ApiError, apiFormRequest, apiRequest, transferTimeoutMs } from './http.ts'
import {
  OrganizationAdmissionAcknowledgmentSchema,
  OrganizationAdmissionQuoteRequestSchema,
  OrganizationAdmissionQuoteSchema,
} from './engine-contracts.generated.ts'
import type { OrganizationAdmissionQuote as EngineOrganizationQuote } from './engine-contracts.generated.ts'

const organizationQuoteFields = {
  organization_name: z.string().trim().min(1, 'Enter your organization name.').max(200, 'Enter an organization name with at most 200 characters.'),
  email: z.email('Enter a valid email address.').max(254, 'Enter an email address with at most 254 characters.'),
}

export const organizationQuoteRequestSchema = OrganizationAdmissionQuoteRequestSchema.extend(organizationQuoteFields)

export const organizationQuoteFormSchema = OrganizationAdmissionQuoteRequestSchema.omit({ phone: true }).extend({
  ...organizationQuoteFields,
  phone_country: z.enum(getCountries()).default(DEFAULT_PHONE_COUNTRY),
  phone_number: z.string().trim().min(1, 'Enter your phone number.'),
}).transform(({ phone_country, phone_number, ...values }, context) => {
  const phone = toE164(phone_country, phone_number)
  if (phone === null) {
    context.addIssue({ code: 'custom', path: ['phone_number'], message: 'Enter a valid phone number.' })
    return z.NEVER
  }
  return { ...values, phone }
})

export type OrganizationQuote = Readonly<EngineOrganizationQuote>
export type OrganizationQuoteRequest = z.infer<typeof organizationQuoteRequestSchema>

const refusalSchema = z.object({ code: z.string() })

/** The engine refuses with `{detail: {code, message}}`, which `http.ts` exposes as `ApiError.detail`; 413 and 415 carry a plain string and so no code. */
export function organizationRefusalCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const refusal = refusalSchema.safeParse(error.detail)
  return refusal.success ? refusal.data.code : null
}

/** Prices the request on the server and stores nothing. */
export function requestOrganizationQuote(input: OrganizationQuoteRequest) {
  return apiRequest('/api/organization-admissions/quote', {
    token: null,
    method: 'POST',
    body: organizationQuoteRequestSchema.parse(input),
    schema: OrganizationAdmissionQuoteSchema,
  })
}

/** Sends the payment receipt for a quote; the engine takes every other value from the signed token. */
export async function submitOrganizationAdmission(quoteToken: string, file: File) {
  const form = new FormData()
  form.append('quote_token', quoteToken)
  form.append('file', file)
  return OrganizationAdmissionAcknowledgmentSchema.parse(
    await apiFormRequest<unknown>('/api/organization-admissions', form, { token: null, timeoutMs: transferTimeoutMs(file.size) }),
  )
}
