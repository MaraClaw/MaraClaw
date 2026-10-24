import { zodResolver } from '@hookform/resolvers/zod'
import { formatDistanceToNowStrict } from 'date-fns'
import { ArrowLeft } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, useParams } from 'react-router-dom'
import { z } from 'zod'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { useTransferDecision, useTransferDetail } from '@/hooks/use-transfer-reviews'
import { ApiError } from '@/lib/http'
import { canOpenTransferAdmission, isPendingTransferPayment } from '@/lib/transfer-review-access'
import { TRANSFER_REASON_LIMIT, transferIdSchema, transferReasonSchema, type TransferDecision, type TransferSummary } from '@/lib/transfer-reviews-api'
import { transferActionClass, transferFeeLabel, transferStatusLabels, transferStatusVariants } from './transfer-payment-labels'

const draftSchema = z.object({ reason: transferReasonSchema })
type DecisionDraft = z.infer<typeof draftSchema>

function PaymentReview({ request, ready }: { readonly request: TransferSummary; readonly ready: boolean }) {
  const { user } = useAuth()
  const fieldId = useId()
  const submitting = useRef(false)
  const outcome = useRef<HTMLHeadingElement>(null)
  const [paymentChecked, setPaymentChecked] = useState(false)
  const decision = useTransferDecision(request.id)
  const form = useForm<DecisionDraft>({ resolver: zodResolver(draftSchema), defaultValues: { reason: '' } })
  const canDecide = ready && isPendingTransferPayment(request) && !decision.isPending
  const conflict = decision.error instanceof ApiError && decision.error.status === 409
  useEffect(() => {
    if (decision.isSuccess) outcome.current?.focus()
  }, [decision.isSuccess])
  const fields = [
    ['Transfer request ID', request.id], ['Applicant user ID', request.applicant_user_id],
    ['Source organization ID', request.source_tenant_id], ['Destination organization ID', request.target_tenant_id],
    ['Transfer choice', request.choice === 'paid' ? 'Paid' : 'Free — no payment decision required'],
    ['Requested', formatDistanceToNowStrict(new Date(request.created_at), { addSuffix: true })],
    ['Payment confirmed', request.payment_confirmed_at ? new Date(request.payment_confirmed_at).toLocaleString() : 'Not confirmed'],
    ['Reviewed', request.reviewed_at ? new Date(request.reviewed_at).toLocaleString() : 'Not reviewed'],
    ['Reviewed by user ID', request.reviewed_by_user_id ?? 'Not reviewed'],
    ['Destination admission ID', request.admission_request_id ?? 'Not created'],
  ] as const

  function submit(value: TransferDecision['decision'], reason: string) {
    if (!canDecide || submitting.current || (value === 'approved' && !paymentChecked)) return
    submitting.current = true
    decision.mutate({ decision: value, reason: reason || null }, { onSettled: () => { submitting.current = false } })
  }

  return <>
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
        <h2 className="font-display text-lg font-semibold">Payment request</h2>
        <Badge variant={transferStatusVariants[request.status]}>{transferStatusLabels[request.status]}</Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <dl className="grid gap-4 sm:grid-cols-2">{fields.map(([label, value]) => <div key={label} className="min-w-0"><dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-1 text-sm leading-relaxed">{value}</dd></div>)}</dl>
        <div><h3 className="text-sm font-medium">Snapshot fee</h3><p className="mt-1 text-sm">{transferFeeLabel(request)}</p><p className="mt-1 text-xs text-muted-foreground">Amount and currency saved when this request was submitted, not the current transfer policy.</p></div>
        <div><h3 className="text-sm font-medium">Snapshot payment instructions</h3><p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">{request.instructions || 'No payment instructions were saved.'}</p></div>
        {request.review_reason ? <div><h3 className="text-sm font-medium">Review reason</h3><p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{request.review_reason}</p></div> : null}
        {request.admission_request_id ? <div className="flex flex-col items-start gap-3">
          <p role="status" className="text-sm leading-relaxed">A destination admission request was created. This is not confirmation of a membership move. The destination organization's genesis administrator must decide admission; its final status is not shown here.</p>
          {canOpenTransferAdmission(user, request) ? <Button asChild variant="outline" className={transferActionClass}><Link to={`/admission-approvals/${request.admission_request_id}`}>Open destination admission</Link></Button> : null}
        </div> : null}
      </CardContent>
    </Card>
    {decision.isSuccess ? <h2 ref={outcome} tabIndex={-1} aria-live="polite" className="rounded-xl border bg-card p-4 text-base font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">Payment review saved: {transferStatusLabels[decision.data.status]}.</h2> : null}
    {decision.isError ? <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">{conflict ? 'This request changed before your decision was saved. The latest details are being refreshed. Check the current status before trying again.' : decision.error instanceof ApiError ? decision.error.message : 'Unable to save your payment decision. Check your connection and retry.'}</p> : null}
    {isPendingTransferPayment(request) ? <Card>
      <CardHeader><h2 className="font-display text-lg font-semibold">Review payment</h2><p className="text-sm leading-relaxed text-muted-foreground">Check payment against the saved fee and instructions before approving. Approval only creates a destination admission request. Rejection does not move the member.</p></CardHeader>
      <CardContent><form noValidate className="flex flex-col gap-4" onSubmit={form.handleSubmit(({ reason }) => submit('approved', reason))}>
        <label htmlFor={`${fieldId}-checked`} className="flex min-h-11 cursor-pointer items-start gap-3 py-2 text-sm leading-relaxed">
          <input id={`${fieldId}-checked`} type="checkbox" checked={paymentChecked} disabled={!canDecide} onChange={(event) => setPaymentChecked(event.currentTarget.checked)} className="mt-1 size-5 shrink-0 accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50" />
          I have checked that payment matches this request's saved fee and instructions.
        </label>
        <div className="flex flex-col gap-2">
          <Label htmlFor={`${fieldId}-reason`}>Reason (optional)</Label>
          <textarea id={`${fieldId}-reason`} rows={4} maxLength={TRANSFER_REASON_LIMIT} disabled={!canDecide} className="w-full min-w-0 resize-y rounded-xl border border-muted-foreground bg-card px-3.5 py-3 text-base text-foreground shadow-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 disabled:opacity-50 sm:text-sm" aria-invalid={Boolean(form.formState.errors.reason)} aria-describedby={`${fieldId}-hint${form.formState.errors.reason ? ` ${fieldId}-error` : ''}`} {...form.register('reason')} />
          <p id={`${fieldId}-hint`} className="text-xs text-muted-foreground">Up to 2,000 characters, saved with the payment decision.</p>
          {form.formState.errors.reason ? <p id={`${fieldId}-error`} role="alert" className="text-sm text-destructive">{form.formState.errors.reason.message}</p> : null}
        </div>
        <div className="flex flex-wrap gap-3">
          <Button type="submit" className={transferActionClass} disabled={!canDecide || !paymentChecked}>{decision.isPending && decision.variables?.decision === 'approved' ? 'Confirming payment...' : 'Confirm payment and create admission'}</Button>
          <Button type="button" variant="destructive" className={transferActionClass} disabled={!canDecide} onClick={form.handleSubmit(({ reason }) => submit('rejected', reason))}>{decision.isPending && decision.variables?.decision === 'rejected' ? 'Rejecting payment...' : 'Reject payment'}</Button>
        </div>
      </form></CardContent>
    </Card> : null}
  </>
}

export function TransferPaymentDetailPage() {
  const { id } = useParams()
  const parsedId = transferIdSchema.safeParse(id)
  const detail = useTransferDetail(parsedId.success ? parsedId.data : null)
  return <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <header className="flex flex-col gap-3">
      <Button asChild variant="outline" className={`self-start ${transferActionClass}`}><Link to="/transfer-payments"><ArrowLeft aria-hidden="true" className="size-4" />Back to transfer payments</Link></Button>
      <h1 className="font-display text-2xl font-semibold tracking-tight">Transfer payment</h1>
    </header>
    {!parsedId.success ? <p role="alert" className="text-sm text-destructive">This transfer payment request link is invalid.</p> : <>
      {detail.isPending ? <Card><CardContent role="status" className="text-sm text-muted-foreground">Loading transfer payment...</CardContent></Card> : null}
      {detail.isError ? <Card><CardContent className="flex flex-col items-start gap-3"><p role="alert" className="text-sm">{detail.error instanceof ApiError ? detail.error.message : 'Unable to load this payment request. Check your connection and retry.'}</p><Button variant="outline" disabled={detail.isFetching} onClick={() => { void detail.refetch() }}>Retry</Button></CardContent></Card> : null}
      {detail.data ? <PaymentReview key={detail.data.id} request={detail.data} ready={detail.isSuccess} /> : null}
    </>}
  </div>
}
