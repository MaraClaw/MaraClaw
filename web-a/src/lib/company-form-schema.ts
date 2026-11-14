import { getCountries } from 'libphonenumber-js/max'
import { z } from 'zod'

import { OrganizationPlanIdValues } from './engine-contracts.generated.ts'
import { toE164 } from './phone.ts'

const companyFields = z.object({
  name: z.string().trim().min(1, 'Enter a company name').max(200, 'Name is too long'),
  phone_country: z.enum(getCountries()),
  phone_number: z.string().trim().min(1, 'Enter the phone number'),
  tier: z.enum(OrganizationPlanIdValues, { error: 'Choose a plan' }),
  admin_email: z
    .string()
    .trim()
    .min(1, 'Enter the org admin email')
    .email('Enter a valid email address')
    .max(254, 'Email is too long'),
  admin_display_name: z.string().trim().max(200, 'Display name is too long').optional(),
  admin_password: z.string().min(6, 'Use at least 6 characters').max(128, 'Password is too long'),
})

const phoneFields = companyFields.pick({ phone_country: true, phone_number: true })

// The number is only valid for its country, so the check spans two fields. `when` keeps it running
// while another field (such as an unchosen plan) fails, so the phone error shows as soon as it is touched.
export const createCompanySchema = companyFields.refine(
  (values) => toE164(values.phone_country, values.phone_number) !== null,
  {
    error: 'Enter a valid phone number',
    path: ['phone_number'],
    when: ({ value }) => phoneFields.safeParse(value).success,
  },
)

export type CreateCompanyValues = z.infer<typeof createCompanySchema>
