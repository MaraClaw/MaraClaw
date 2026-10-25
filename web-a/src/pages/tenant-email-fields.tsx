import { useId } from 'react'
import type { UseFormReturn } from 'react-hook-form'
import { z } from 'zod'

import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { TenantEmailDraft, tenantEmailDraftSchema } from './tenant-email-form'

type Props = {
  readonly form: UseFormReturn<TenantEmailDraft, unknown, z.output<typeof tenantEmailDraftSchema>>
  readonly hasPassword: boolean
}

export function TenantEmailFields({ form, hasPassword }: Props) {
  const id = useId()
  const { register, formState: { errors } } = form
  return <>
    <div>
      <Label htmlFor={`${id}-enabled`} className="flex min-h-11 cursor-pointer items-center gap-3">
        <input id={`${id}-enabled`} type="checkbox" className="size-5 accent-primary focus-visible:outline-2 focus-visible:outline-ring" {...register('enabled')} />
        Enable organization SMTP
      </Label>
      <p className="text-sm text-muted-foreground">When off, this organization sends no email. Its saved SMTP configuration can still be tested.</p>
    </div>
    <div className="grid min-w-0 gap-5 sm:grid-cols-2">
      {([
        ['from_address', 'Sender email address', 'email', 'notifications@example.com'],
        ['from_name', 'Sender name', 'text', 'Organization name'],
        ['smtp_host', 'SMTP host', 'text', 'smtp.example.com'],
        ['smtp_port', 'SMTP port', 'text', '465'],
        ['smtp_username', 'SMTP username', 'text', ''],
        ['smtp_timeout_seconds', 'Timeout (seconds)', 'text', '15'],
      ] as const).map(([name, label, type, placeholder]) => <div key={name} className="min-w-0 space-y-2">
        <Label htmlFor={`${id}-${name}`}>{label}</Label>
        <Input id={`${id}-${name}`} type={type} placeholder={placeholder} inputMode={name === 'smtp_port' || name === 'smtp_timeout_seconds' ? 'numeric' : name === 'from_address' ? 'email' : 'text'} autoCapitalize="none" autoComplete="off" spellCheck={false} aria-invalid={Boolean(errors[name])} aria-describedby={errors[name] ? `${id}-${name}-error` : undefined} {...register(name)} />
        {errors[name] ? <p id={`${id}-${name}-error`} role="alert" className="text-sm text-destructive">{errors[name].message}</p> : null}
      </div>)}
    </div>
    <div className="space-y-2">
      <Label htmlFor={`${id}-password`}>SMTP password</Label>
      <Input id={`${id}-password`} type="password" autoComplete="new-password" autoCapitalize="none" spellCheck={false} aria-describedby={`${id}-password-help`} {...register('password')} />
      <p id={`${id}-password-help`} className="text-sm text-muted-foreground">{hasPassword ? 'A password is saved. Leave blank to keep it; enter a new password to replace it.' : 'No password is saved. Leave blank if the SMTP server does not require one.'} Saved passwords are never displayed.</p>
    </div>
    <div>
      <Label htmlFor={`${id}-ssl`} className="flex min-h-11 cursor-pointer items-center gap-3">
        <input id={`${id}-ssl`} type="checkbox" className="size-5 accent-primary focus-visible:outline-2 focus-visible:outline-ring" aria-describedby={`${id}-ssl-help`} {...register('smtp_ssl')} />
        Use implicit TLS (SSL)
      </Label>
      <p id={`${id}-ssl-help`} className="text-sm text-muted-foreground">Usually port 465. When off, the connection uses opportunistic STARTTLS or plaintext; encryption is not guaranteed.</p>
    </div>
  </>
}
