import { z } from 'zod'

import { OrganizationPlanIdValues } from './engine-contracts.generated.ts'

export const createCompanySchema = z.object({
  name: z.string().trim().min(1, 'Enter a company name').max(200, 'Name is too long'),
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

export type CreateCompanyValues = z.infer<typeof createCompanySchema>
