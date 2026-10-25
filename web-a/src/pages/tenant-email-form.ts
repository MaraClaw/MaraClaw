import { z } from 'zod'

import type { TenantEmail } from '../lib/tenant-email-api.ts'

const port = z.string().trim().regex(/^\d+$/, 'Enter a whole number from 1 to 65535.').transform(Number).pipe(z.number().int().min(1).max(65535))
const timeout = z.string().trim().regex(/^\d+$/, 'Enter a whole number from 1 to 30.').transform(Number).pipe(z.number().int().min(1).max(30))

export const tenantEmailDraftSchema = z.object({
  enabled: z.boolean(),
  from_address: z.string().trim(),
  from_name: z.string().trim(),
  smtp_host: z.string().trim(),
  smtp_port: port,
  smtp_username: z.string().trim(),
  password: z.string(),
  smtp_ssl: z.boolean(),
  smtp_timeout_seconds: timeout,
}).superRefine((draft, context) => {
  if (!draft.enabled) return
  if (!z.email().safeParse(draft.from_address).success) context.addIssue({ code: 'custom', path: ['from_address'], message: 'Enter a valid sender email address when organization SMTP is enabled.' })
  if (!z.hostname().safeParse(draft.smtp_host).success && !z.ipv4().safeParse(draft.smtp_host).success && !z.ipv6().safeParse(draft.smtp_host).success) context.addIssue({ code: 'custom', path: ['smtp_host'], message: 'Enter an SMTP hostname or IP address without a protocol, path, or spaces.' })
}).transform(({ password, ...fields }) => password ? { ...fields, password } : fields)

export type TenantEmailDraft = z.input<typeof tenantEmailDraftSchema>

export function tenantEmailToDraft(saved: TenantEmail): TenantEmailDraft {
  return {
    enabled: saved.enabled, from_address: saved.from_address, from_name: saved.from_name,
    smtp_host: saved.smtp_host, smtp_port: String(saved.smtp_port), smtp_username: saved.smtp_username,
    password: '', smtp_ssl: saved.smtp_ssl, smtp_timeout_seconds: String(saved.smtp_timeout_seconds),
  }
}

export function tenantEmailSource(source: TenantEmail['effective_source']): string {
  switch (source) {
    case 'tenant': return 'Organization SMTP is used for this organization’s email.'
    case 'none': return 'Organization email delivery is off. Save and enable this organization’s SMTP configuration to send organization email.'
    default: { const unreachable: never = source; return unreachable }
  }
}
