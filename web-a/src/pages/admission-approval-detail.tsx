import { zodResolver } from '@hookform/resolvers/zod'
import { useTranslation } from '@/hooks/use-translation'
import { ArrowLeft } from 'lucide-react'
import { type ComponentProps, useId, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { Link, useParams } from 'react-router-dom'
import { z } from 'zod'

import { Badge } from '@/components/ui/badge'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { useAdmissionDecision, useAdmissionDetail } from '@/hooks/use-admissions'
import {
  ADMISSION_REASON_LIMIT, admissionIdSchema, admissionReasonSchema,
  type AdmissionDecision, type AdmissionDetail,
} from '@/lib/admissions-api'
import { ApiError } from '@/lib/http'
import { admissionMethodLabels, admissionStatusLabels } from './admission-labels'

const reasonDraftSchema = z.object({ reason: admissionReasonSchema })
type ReasonDraft = z.infer<typeof reasonDraftSchema>
const actionClass = 'h-auto min-h-11 whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'
const statusVariants = {
  pending: 'warning',
  approved: 'success',
  rejected: 'destructive',
  withdrawn: 'secondary',
} as const satisfies Record<AdmissionDetail['status'], ComponentProps<typeof Badge>['variant']>

function AdmissionReview({ request, ready }: { readonly request: AdmissionDetail; readonly ready: boolean }) {
  const { t, distance, owned } = useTranslation()
  const reasonId = useId()
  const submitting = useRef(false)
  const [confirmed, setConfirmed] = useState(false)
  const decision = useAdmissionDecision(request.id)
  const form = useForm<ReasonDraft>({ resolver: zodResolver(reasonDraftSchema), defaultValues: { reason: '' } })
  const canDecide = ready && request.status === 'pending' && !decision.isPending
  const conflict = decision.error instanceof ApiError && decision.error.status === 409
  const fields = [
    ['Applicant email', request.applicant.email ?? t('Not provided')],
    ['Applicant ID', request.applicant.id],
    ['Source organization', request.source_tenant?.name ?? t('No source organization')],
    ['Target organization', request.target_tenant.name],
    ['Admission method', t(admissionMethodLabels[request.method])],
    ['Requested', distance(request.created_at)],
    ['Decided', request.decided_at ? distance(request.decided_at) : t('Not decided')],
    ['Decided by user ID', request.decided_by_user_id ?? t('Not decided')],
  ] as const

  function submit(value: AdmissionDecision['decision'], reason: string) {
    if (!canDecide || (value === 'approved' && !confirmed) || submitting.current) return
    submitting.current = true
    decision.mutate({ decision: value, reason: reason || null }, { onSettled: () => { submitting.current = false; setConfirmed(false) } })
  }

  return (
    <>
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
          <h2 className="font-display text-lg font-semibold">{t('Request details')}</h2>
          <Badge variant={statusVariants[request.status]}>{t(admissionStatusLabels[request.status])}</Badge>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <dl className="grid gap-4 sm:grid-cols-2">
            {fields.map(([label, value]) => <div key={label} className="min-w-0">
              <dt className="text-xs text-muted-foreground">{t(label)}</dt>
              <dd className="mt-1 text-sm leading-relaxed">{value}</dd>
            </div>)}
          </dl>
          {request.decision_reason ? <div><h3 className="text-sm font-medium">{t('Decision reason')}</h3><p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{request.decision_reason}</p></div> : null}
        </CardContent>
      </Card>
      <p role="status" className="sr-only">{decision.isSuccess ? t('Request {status}.', { status: t(admissionStatusLabels[decision.data.status]).toLowerCase() }) : ''}</p>
      {decision.isError ? <ErrorToast error={decision.error} message={conflict ? t('This request changed before your decision was saved. The latest details are being refreshed. Review the current status before trying again.') : decision.error instanceof ApiError ? decision.error.message : t('Unable to save your decision. Try again.')} /> : null}
      {request.status === 'pending' ? <Card>
        <CardHeader><h2 className="font-display text-lg font-semibold">{t('Review decision')}</h2><p className="text-sm leading-relaxed text-muted-foreground">{t('Approve to admit this applicant to {name}, or reject the request.', { name: request.target_tenant.name })}</p></CardHeader>
        <CardContent>
          <form noValidate className="flex flex-col gap-4" onSubmit={form.handleSubmit(({ reason }) => submit('approved', reason))}>
            <div className="flex flex-col gap-2">
              <Label htmlFor={reasonId}>{t('Reason (optional)')}</Label>
              <textarea id={reasonId} rows={4} maxLength={ADMISSION_REASON_LIMIT} disabled={!canDecide}
                className="w-full min-w-0 resize-y rounded-xl border border-muted-foreground bg-card px-3.5 py-3 text-base text-foreground shadow-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 disabled:opacity-50 sm:text-sm"
                aria-invalid={Boolean(form.formState.errors.reason)} aria-describedby={`${reasonId}-hint${form.formState.errors.reason ? ` ${reasonId}-error` : ''}`} {...form.register('reason')} />
              <p id={`${reasonId}-hint`} className="text-xs text-muted-foreground">{t('Up to 2,000 characters. This reason is saved with the decision.')}</p>
              {form.formState.errors.reason ? <p id={`${reasonId}-error`} role="alert" className="text-sm text-destructive">{owned(form.formState.errors.reason.message)}</p> : null}
            </div>
            <label htmlFor={`${reasonId}-confirm`} className="flex min-h-11 cursor-pointer items-start gap-3 py-2 text-sm leading-relaxed">
              <input id={`${reasonId}-confirm`} type="checkbox" checked={confirmed} disabled={!canDecide} onChange={(event) => setConfirmed(event.currentTarget.checked)} className="mt-1 size-5 shrink-0 accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50" />
              {t('I understand that approval admits this applicant to {name}, and that a saved decision cannot be changed.', { name: request.target_tenant.name })}
            </label>
            <div className="flex flex-wrap gap-3">
              <Button type="submit" className={actionClass} disabled={!canDecide || !confirmed}>{t(decision.isPending && decision.variables?.decision === 'approved' ? 'Approving...' : 'Approve request')}</Button>
              <Button type="button" variant="outline" className={actionClass} disabled={!canDecide} onClick={form.handleSubmit(({ reason }) => submit('rejected', reason))}>{t(decision.isPending && decision.variables?.decision === 'rejected' ? 'Rejecting...' : 'Reject request')}</Button>
            </div>
          </form>
        </CardContent>
      </Card> : null}
    </>
  )
}

export function AdmissionApprovalDetailPage() {
  const { t } = useTranslation()
  const { id } = useParams()
  const parsedId = admissionIdSchema.safeParse(id)
  const detail = useAdmissionDetail(parsedId.success ? parsedId.data : null)

  return (
    <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
      <header className="flex flex-col gap-3">
        <Button asChild variant="outline" className="self-start">
          <Link to="/admission-approvals"><ArrowLeft aria-hidden="true" className="size-4" />{t('Back to admission approvals')}</Link>
        </Button>
        <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Admission request')}</h1>
      </header>
      {!parsedId.success ? <p role="alert" className="text-sm text-destructive">{t('This admission request link is invalid.')}</p> : <>
        {detail.isPending ? <Card><CardContent role="status" className="text-sm text-muted-foreground">{t('Loading admission request...')}</CardContent></Card> : null}
        {detail.isError ? <Card><CardContent className="flex flex-col items-start gap-3">
          <ErrorToast error={detail.error} message={detail.error instanceof ApiError ? detail.error.message : t('Unable to load this admission request.')} />
          <Button variant="outline" className={actionClass} disabled={detail.isFetching} onClick={() => { void detail.refetch() }}>{t('Retry')}</Button>
        </CardContent></Card> : null}
        {detail.data ? <AdmissionReview key={detail.data.id} request={detail.data} ready={detail.isSuccess && !detail.isFetching} /> : null}
      </>}
    </div>
  )
}
