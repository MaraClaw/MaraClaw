import { zodResolver } from '@hookform/resolvers/zod'
import { ArrowLeftRight, Building2, Lock } from 'lucide-react'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, Navigate } from 'react-router-dom'
import { z } from 'zod'

import { AuthShell } from '@/components/auth/auth-shell'
import { AdmissionPending } from '@/components/auth/admission-pending'
import { TransferChoice } from '@/components/auth/transfer-choice'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { useTransferFunnel } from '@/hooks/use-transfer-funnel'
import { formatMoney } from '@/lib/format-money'

const transferSchema = z.object({
  invitation_code: z.string().trim().max(32).optional(),
  password: z.string().min(1, 'Confirm your current password').max(128),
})
const highlights = [
  { icon: ArrowLeftRight, title: 'Move with an invite', body: 'Paste a company invitation code and confirm your password.' },
  { icon: Building2, title: 'Or match your email', body: 'Request admission to the company that claimed your domain.' },
  { icon: Lock, title: 'Your choice, before you submit', body: 'Review the paid and free options before transferring agents.' },
]

export function TransferPage() {
  const { status, owner } = useAuth()
  const [session, setSession] = useState({ owner, generation: 0 })
  if (session.owner !== owner) {
    setSession({ owner, generation: session.generation + 1 })
    return null
  }
  if (status === 'anonymous') return <Navigate to="/login" replace />
  if (status === 'loading') return <p role="status" className="p-6 text-sm text-muted-foreground">Loading…</p>
  return <TransferForm key={session.generation} />
}

function TransferForm() {
  const formId = useId()
  const flow = useTransferFunnel()
  const { register, handleSubmit, getValues, formState: { errors } } = useForm<z.infer<typeof transferSchema>>({
    resolver: zodResolver(transferSchema), defaultValues: { invitation_code: '', password: '' },
  })
  const disabled = flow.busy || !flow.quote?.source_eligible || Boolean(flow.admission || flow.payment)
  return (
    <AuthShell headingId={`${formId}-heading`} title="Transfer organization"
      description="Default-organization members can request a company transfer. Stay in your current workspace until all reviews are complete."
      brandTitle="Change where you work" brandBody="Keep the same MaraClaw account and request admission with an invite or a matching domain." highlights={highlights}>
      {!flow.quote && !flow.quoteError ? <p role="status" className="mb-4 text-sm text-muted-foreground">Loading transfer options…</p> : null}
      {flow.quoteError ? <div role="alert" className="mb-4 space-y-2 text-sm text-destructive"><p>{flow.quoteError}</p><Button variant="outline" onClick={flow.reload}>Retry transfer options</Button></div> : null}
      {flow.quote && !flow.quote.source_eligible ? <p role="alert" className="mb-4 text-sm text-destructive">Only default-organization members can transfer to a company. Transfers from one company to another are not supported.</p> : null}
      {flow.admission ? <AdmissionPending request={flow.admission} /> : null}
      {flow.payment ? <div role="status" className="mb-4 space-y-2 rounded-xl border border-border bg-muted/50 p-4 text-sm">
        <p className="font-medium">Awaiting payment review</p>
        <p>Your organization has not changed. Payment approval is separate from destination company approval.</p>
        <p>Saved fee: {formatMoney(flow.payment.fee_amount_minor, flow.payment.currency)} ({flow.payment.currency})</p>
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{flow.payment.instructions}</p>
        <Link className="inline-flex min-h-11 items-center font-medium text-primary hover:underline" to={`/app/admissions/transfers/${encodeURIComponent(flow.payment.id)}`}>View transfer request</Link>
      </div> : null}
      {flow.error && !flow.selection ? <p role="alert" className="mb-4 text-sm text-destructive">{flow.error}</p> : null}
      <form className="space-y-4" aria-labelledby={`${formId}-heading`} onSubmit={handleSubmit((values) => {
        const code = values.invitation_code
        if (!code) { flow.setError('Enter an invitation code, or choose a destination below.'); return }
        flow.submit({ password: values.password, invitation_code: code })
      })}>
        <div className="space-y-2">
          <Label htmlFor={`${formId}-invite`}>Invitation code</Label>
          <Input id={`${formId}-invite`} placeholder="Optional" maxLength={32} aria-invalid={Boolean(errors.invitation_code)} {...register('invitation_code')} />
        </div>
        <div className="space-y-2">
          <Label htmlFor={`${formId}-password`}>Current password</Label>
          <Input id={`${formId}-password`} type="password" autoComplete="current-password" maxLength={128} aria-invalid={Boolean(errors.password)} aria-describedby={errors.password ? `${formId}-password-error` : undefined} {...register('password')} />
          {errors.password ? <p id={`${formId}-password-error`} className="text-xs text-destructive">{errors.password.message}</p> : null}
        </div>
        <Button type="submit" className="h-11 w-full" disabled={disabled}>{flow.busy ? 'Submitting…' : 'Transfer with invite'}</Button>
      </form>
      {flow.match ? <Button variant="outline" className="mt-4 h-auto min-h-11 w-full whitespace-normal" disabled={disabled} onClick={() => {
        const values = transferSchema.safeParse(getValues())
        if (!values.success) { flow.setError('Enter your current password first.'); return }
        if (flow.match) flow.submit({ password: values.data.password, tenant_id: flow.match.id })
      }}>Transfer to {flow.match.name}</Button> : null}
      <div className="mt-6 flex flex-wrap justify-center gap-4 text-sm">
        <Link to="/app" className="inline-flex min-h-11 items-center font-medium text-primary hover:underline">Back to workspace</Link>
        <Link to="/app/admissions" className="inline-flex min-h-11 items-center font-medium text-primary hover:underline">View saved requests</Link>
      </div>
      {flow.selection && flow.quote ? <TransferChoice quote={flow.quote} busy={flow.busy} error={flow.error} onCancel={flow.cancel} onConfirm={flow.confirm} /> : null}
    </AuthShell>
  )
}
