import { zodResolver } from '@hookform/resolvers/zod'
import { ArrowLeftRight, Building2, Lock } from 'lucide-react'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, Navigate } from 'react-router-dom'
import { z } from 'zod'

import { AuthShell } from '@/components/auth/auth-shell'
import { AdmissionPending } from '@/components/auth/admission-pending'
import { InvitationCode } from '@/components/auth/invitation-code'
import { TransferChoice } from '@/components/auth/transfer-choice'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { useInvitationLink } from '@/hooks/use-invitation-link'
import { useInvitationPreview } from '@/hooks/use-invitation-preview'
import { useTransferFunnel } from '@/hooks/use-transfer-funnel'
import { formatMoney } from '@/lib/format-money'
import { userMustChangePassword } from '@/lib/types/auth'

const transferSchema = z.object({ password: z.string().min(1, 'Confirm your current password').max(128) })
const highlights = [
  { icon: ArrowLeftRight, title: 'Start with your invitation', body: 'Validate the code sent to your email before seeing the destination.' },
  { icon: Building2, title: 'Confirm the destination', body: 'A valid invitation lets you join without a separate company approval.' },
  { icon: Lock, title: 'Your choice, before you submit', body: 'Confirm your password and review paid and free transfer options.' },
]

export function TransferPage() {
  const { status, user } = useAuth()
  const code = useInvitationLink()
  if (status === 'anonymous') return <Navigate to="/login" replace state={{ from: { pathname: code ? `/transfer#code=${encodeURIComponent(code)}` : '/transfer' } }} />
  if (status === 'loading') return <p role="status" className="p-6 text-sm text-muted-foreground">Loading…</p>
  if (userMustChangePassword(user)) return <Navigate to="/app/settings" replace />
  if (!user?.tenant_id) return <Navigate to="/join" replace state={{ invitationCode: code }} />
  return <TransferSession initialCode={code} />
}

function TransferSession({ initialCode }: { readonly initialCode: string }) {
  const { owner } = useAuth()
  const [session, setSession] = useState({ owner, generation: 0 })
  if (session.owner !== owner) {
    setSession({ owner, generation: session.generation + 1 })
    return null
  }
  return <TransferForm key={session.generation} initialCode={session.generation === 0 ? initialCode : ''} />
}

function TransferForm({ initialCode }: { readonly initialCode: string }) {
  const formId = useId()
  const flow = useTransferFunnel()
  const invitation = useInvitationPreview(initialCode)
  const { register, handleSubmit, formState: { errors } } = useForm<z.infer<typeof transferSchema>>({
    resolver: zodResolver(transferSchema), defaultValues: { password: '' },
  })
  const disabled = flow.busy || !flow.quote?.source_eligible || Boolean(flow.admission || flow.payment)
  return <AuthShell headingId={`${formId}-heading`} title="Transfer organization"
    description="Validate your invitation, then confirm your password and transfer choices. Free transfers join immediately; paid transfers join after payment approval."
    brandTitle="Change where you work" brandBody="Keep the same MaraClaw account and join the company that invited you." highlights={highlights}>
    {!flow.quote && !flow.quoteError ? <p role="status" className="mb-4 text-sm text-muted-foreground">Loading transfer options…</p> : null}
    {flow.quoteError ? <div role="alert" className="mb-4 space-y-2 text-sm text-destructive"><p>{flow.quoteError}</p><Button variant="outline" onClick={flow.reload}>Retry transfer options</Button></div> : null}
    {flow.quote && !flow.quote.source_eligible ? <p role="alert" className="mb-4 text-sm text-destructive">Only default-organization members can transfer to a company. Transfers from one company to another are not supported.</p> : null}
    {flow.admission?.status === 'pending' ? <AdmissionPending request={flow.admission} /> : null}
    {flow.admission?.status === 'approved' ? <div role="status" className="mb-4 space-y-2 text-sm"><p>Organization joined. {flow.busy ? 'Opening workspace…' : 'Open your joined workspace to continue.'}</p><Button disabled={flow.busy} onClick={() => void flow.open()}>Open joined workspace</Button></div> : null}
    {flow.payment ? <div role="status" className="mb-4 space-y-2 rounded-xl border border-border bg-muted/50 p-4 text-sm">
      <p className="font-medium">Awaiting payment review</p><p>Your current workspace stays available. Payment approval joins the destination automatically; no separate company approval is needed.</p>
      <p>Saved fee: {formatMoney(flow.payment.fee_amount_minor, flow.payment.currency)} ({flow.payment.currency})</p>
      <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{flow.payment.instructions}</p>
      <Link className="inline-flex min-h-11 items-center font-medium text-primary hover:underline" to={`/app/admissions/transfers/${encodeURIComponent(flow.payment.id)}`}>View transfer request</Link>
    </div> : null}
    {flow.error && !flow.selection ? <p role="alert" className="mb-4 text-sm text-destructive">{flow.error}</p> : null}
    <InvitationCode invitation={invitation} disabled={disabled} />
    {invitation.preview && !flow.admission && !flow.payment ? <form className="space-y-4" aria-labelledby={`${formId}-heading`} onSubmit={handleSubmit((values) => {
      if (invitation.preview) flow.submit({ password: values.password, invitation_code: invitation.code.trim() })
    })}>
      <div className="space-y-2"><Label htmlFor={`${formId}-password`}>Current password</Label>
        <Input id={`${formId}-password`} type="password" autoComplete="current-password" maxLength={128} disabled={disabled} aria-invalid={Boolean(errors.password)} aria-describedby={errors.password ? `${formId}-password-error` : undefined} {...register('password')} />
        {errors.password ? <p id={`${formId}-password-error`} className="text-xs text-destructive">{errors.password.message}</p> : null}
      </div>
      <Button type="submit" className="h-11 w-full" disabled={disabled}>{flow.busy ? 'Submitting…' : 'Continue transfer'}</Button>
    </form> : null}
    <div className="mt-6 flex flex-wrap justify-center gap-4 text-sm">
      <Link to="/app" className="inline-flex min-h-11 items-center font-medium text-primary hover:underline">Back to workspace</Link>
      <Link to="/app/admissions" className="inline-flex min-h-11 items-center font-medium text-primary hover:underline">View saved requests</Link>
    </div>
    {flow.selection && flow.quote ? <TransferChoice quote={flow.quote} busy={flow.busy} error={flow.error} onCancel={flow.cancel} onConfirm={flow.confirm} /> : null}
  </AuthShell>
}
