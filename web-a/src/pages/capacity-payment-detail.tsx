import { zodResolver } from '@hookform/resolvers/zod'
import { ArrowLeft } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, useParams } from 'react-router-dom'
import type { z } from 'zod'

import { CapacityEvidencePreview } from '@/components/capacity/capacity-evidence-preview'
import { capacityActionClass, capacityStatusLabels } from '@/components/capacity/capacity-labels'
import { CapacityOrderDetails } from '@/components/capacity/capacity-order-details'
import { CapacityStatusBadge } from '@/components/capacity/capacity-status-badge'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { useCapacityDecision, useCapacityReviewOrder } from '@/hooks/use-capacity'
import { useTranslation } from '@/hooks/use-translation'
import { CAPACITY_DECISION_NOTE_LIMIT, capacityOrderIdSchema, type CapacityOrder } from '@/lib/capacity-api'
import { capacityGuardKey } from '@/lib/capacity-errors'
import { capacityDecisionDraftSchema } from '@/lib/capacity-forms'
import { formatVnd } from '@/lib/capacity-format'
import { isDecidable } from '@/lib/capacity-order-state'
import { ApiError } from '@/lib/http'

type DecisionDraft = z.input<typeof capacityDecisionDraftSchema>

function PaymentReview({ order, ready }: { readonly order: CapacityOrder; readonly ready: boolean }) {
  const { t, locale, owned } = useTranslation()
  const fieldId = useId()
  const submitting = useRef(false)
  const outcome = useRef<HTMLHeadingElement>(null)
  const [checked, setChecked] = useState(false)
  const decision = useCapacityDecision(order.id)
  const form = useForm<DecisionDraft>({ resolver: zodResolver(capacityDecisionDraftSchema), defaultValues: { note: '' } })
  const conflict = decision.error instanceof ApiError && decision.error.status === 409
  const refused = capacityGuardKey(decision.error)
  const canDecide = ready && isDecidable(order) && !decision.isPending
  const noteError = form.formState.errors.note
  useEffect(() => { if (decision.isSuccess) outcome.current?.focus() }, [decision.isSuccess])

  function submit(action: 'approve' | 'reject', note: string) {
    if (!canDecide || submitting.current) return
    if (action === 'approve' && !checked) return
    if (action === 'reject' && note === '') {
      form.setError('note', { message: 'Enter a note explaining the rejection.' })
      return
    }
    submitting.current = true
    decision.mutate({ action, ...(note ? { note } : {}) }, {
      onSettled: () => { submitting.current = false; setChecked(false) },
    })
  }

  return <>
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
        <h2 className="font-display text-lg font-semibold">{order.tenant_name ?? t('Unknown company')}</h2>
        <CapacityStatusBadge status={order.status} />
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <CapacityOrderDetails order={order} />
        <div>
          <h3 className="text-sm font-medium">{t('Recipient saved with this order')}</h3>
          <dl className="mt-2 grid gap-4 sm:grid-cols-3">
            <div className="min-w-0"><dt className="text-xs text-muted-foreground">{t('Bank name')}</dt><dd className="mt-1 text-sm">{order.recipient.bank_name}</dd></div>
            <div className="min-w-0"><dt className="text-xs text-muted-foreground">{t('Account number')}</dt><dd className="mt-1 text-sm tabular-nums">{order.recipient.account_number}</dd></div>
            <div className="min-w-0"><dt className="text-xs text-muted-foreground">{t('Branch name')}</dt><dd className="mt-1 text-sm">{order.recipient.branch_name}</dd></div>
          </dl>
        </div>
        {order.review_note ? <div>
          <h3 className="text-sm font-medium">{t('Reviewer note')}</h3>
          <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{order.review_note}</p>
        </div> : null}
      </CardContent>
    </Card>
    <Card>
      <CardHeader><h2 className="font-display text-lg font-semibold">{t('Payment evidence')}</h2></CardHeader>
      <CardContent>
        {order.evidence !== null
          ? <CapacityEvidencePreview orderId={order.id} evidence={order.evidence} />
          : <p className="text-sm text-muted-foreground">{t('No payment evidence has been sent for this order.')}</p>}
      </CardContent>
    </Card>
    {decision.isSuccess ? <h2 ref={outcome} tabIndex={-1} aria-live="polite" className="rounded-xl border bg-card p-4 text-base font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">{t('Decision saved: {status}.', { status: t(capacityStatusLabels[decision.data.status]) })}</h2> : null}
    {decision.isError ? <ErrorToast error={decision.error} message={conflict
      ? t('This order changed before your decision was saved. The latest details are being refreshed. Check the current status before trying again.')
      : refused !== null ? t(refused)
        : decision.error instanceof ApiError ? decision.error.message : t('Could not save your decision. Check your connection and retry.')} /> : null}
    {isDecidable(order) ? <Card>
      <CardHeader>
        <h2 className="font-display text-lg font-semibold">{t('Review payment')}</h2>
        <p className="text-sm leading-relaxed text-muted-foreground">{t('Approving makes the extra accounts and agents count toward the company limits. Rejecting needs a note, which the company admins can read.')}</p>
      </CardHeader>
      <CardContent>
        <form noValidate className="flex flex-col gap-4" onSubmit={form.handleSubmit(({ note }) => submit('approve', note))}>
          <label htmlFor={`${fieldId}-checked`} className="flex min-h-11 cursor-pointer items-start gap-3 py-2 text-sm leading-relaxed">
            <input id={`${fieldId}-checked`} type="checkbox" checked={checked} disabled={!canDecide} onChange={(event) => setChecked(event.currentTarget.checked)} className="mt-1 size-5 shrink-0 accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50" />
            {t('I checked that the evidence shows a transfer of {amount} to this account with the reference {reference}.', { amount: formatVnd(order.amount_vnd, locale), reference: order.reference })}
          </label>
          <div className="flex flex-col gap-2">
            <Label htmlFor={`${fieldId}-note`}>{t('Note (required to reject)')}</Label>
            <textarea id={`${fieldId}-note`} rows={4} maxLength={CAPACITY_DECISION_NOTE_LIMIT} disabled={!canDecide} className="w-full min-w-0 resize-y rounded-xl border border-muted-foreground bg-card px-3.5 py-3 text-base text-foreground shadow-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 disabled:opacity-50 sm:text-sm" aria-invalid={Boolean(noteError)} aria-describedby={`${fieldId}-hint${noteError ? ` ${fieldId}-error` : ''}`} {...form.register('note')} />
            <p id={`${fieldId}-hint`} className="text-xs text-muted-foreground">{t('Up to 1,000 characters, saved with the decision.')}</p>
            {noteError ? <p id={`${fieldId}-error`} role="alert" className="text-sm text-destructive">{owned(noteError.message)}</p> : null}
          </div>
          <p role="status" className="text-sm text-muted-foreground">{decision.isPending ? t('Saving your decision...') : ''}</p>
          <div className="flex flex-wrap gap-3">
            <Button type="submit" className={capacityActionClass} disabled={!canDecide || !checked}>{t(decision.isPending && decision.variables?.action === 'approve' ? 'Approving...' : 'Approve payment')}</Button>
            <Button type="button" variant="destructive" className={capacityActionClass} disabled={!canDecide} onClick={form.handleSubmit(({ note }) => submit('reject', note))}>{t(decision.isPending && decision.variables?.action === 'reject' ? 'Rejecting...' : 'Reject payment')}</Button>
          </div>
        </form>
      </CardContent>
    </Card> : null}
  </>
}

export function CapacityPaymentDetailPage() {
  const { t } = useTranslation()
  const { orderId } = useParams()
  const parsedId = capacityOrderIdSchema.safeParse(orderId)
  const detail = useCapacityReviewOrder(parsedId.success ? parsedId.data : null)

  return <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <header className="flex flex-col gap-3">
      <Button asChild variant="outline" className={`self-start ${capacityActionClass}`}><Link to="/capacity-payments"><ArrowLeft aria-hidden="true" className="size-4" />{t('Back to capacity payments')}</Link></Button>
      <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Capacity payment')}</h1>
    </header>
    {!parsedId.success ? <p role="alert" className="text-sm text-destructive">{t('This payment link is invalid.')}</p> : <>
      {detail.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('Loading payment...')}</p> : null}
      {detail.isError ? <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-sm text-destructive">{detail.error instanceof ApiError
          ? detail.error.status === 404 ? t('This payment does not exist.') : detail.error.message
          : t('Could not load this payment. Check your connection and retry.')}</p>
        <Button variant="outline" className={capacityActionClass} disabled={detail.isFetching} onClick={() => { void detail.refetch() }}>{t('Retry')}</Button>
      </div> : null}
      {detail.data ? <PaymentReview key={detail.data.id} order={detail.data} ready={detail.isSuccess} /> : null}
    </>}
  </div>
}
