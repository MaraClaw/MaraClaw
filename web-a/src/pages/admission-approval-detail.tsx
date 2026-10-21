import { zodResolver } from '@hookform/resolvers/zod'
import { formatDistanceToNowStrict } from 'date-fns'
import { useId, useRef } from 'react'
import { useForm } from 'react-hook-form'
import { Link, useParams } from 'react-router-dom'
import { z } from 'zod'

import { Badge } from '@/components/ui/badge'
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

function AdmissionReview({ request, ready }: { readonly request: AdmissionDetail; readonly ready: boolean }) {
  const reasonId = useId()
  const submitting = useRef(false)
  const decision = useAdmissionDecision(request.id)
  const form = useForm<ReasonDraft>({ resolver: zodResolver(reasonDraftSchema), defaultValues: { reason: '' } })
  const canDecide = ready && request.status === 'pending' && !decision.isPending
  const conflict = decision.error instanceof ApiError && decision.error.status === 409
  const fields = [
    ['Applicant email', request.applicant.email ?? 'Not provided'],
    ['Applicant ID', request.applicant.id],
    ['Source organization', request.source_tenant?.name ?? 'No source organization'],
    ['Target organization', request.target_tenant.name],
    ['Admission method', admissionMethodLabels[request.method]],
    ['Requested', formatDistanceToNowStrict(new Date(request.created_at), { addSuffix: true })],
    ['Decided', request.decided_at ? formatDistanceToNowStrict(new Date(request.decided_at), { addSuffix: true }) : 'Not decided'],
    ['Decided by user ID', request.decided_by_user_id ?? 'Not decided'],
  ] as const

  function submit(value: AdmissionDecision['decision'], reason: string) {
    if (!canDecide || submitting.current) return
    submitting.current = true
    decision.mutate({ decision: value, reason: reason || null }, { onSettled: () => { submitting.current = false } })
  }

  return (
    <>
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
          <h2 className="font-display text-lg font-semibold">Request details</h2>
          <Badge variant={request.status === 'pending' ? 'warning' : 'secondary'}>{admissionStatusLabels[request.status]}</Badge>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <dl className="grid gap-4 sm:grid-cols-2">
            {fields.map(([label, value]) => <div key={label} className="min-w-0">
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="mt-1 text-sm leading-relaxed">{value}</dd>
            </div>)}
          </dl>
          {request.decision_reason ? <div><h3 className="text-sm font-medium">Decision reason</h3><p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground">{request.decision_reason}</p></div> : null}
        </CardContent>
      </Card>
      <p role="status" className="sr-only">{decision.isSuccess ? `Request ${admissionStatusLabels[decision.data.status].toLowerCase()}.` : ''}</p>
      {decision.isError ? <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
        {conflict ? 'This request changed before your decision was saved. The latest details are being refreshed. Review the current status before trying again.' : decision.error instanceof ApiError ? decision.error.message : 'Unable to save your decision. Try again.'}
      </p> : null}
      {request.status === 'pending' ? <Card>
        <CardHeader><h2 className="font-display text-lg font-semibold">Review decision</h2><p className="text-sm leading-relaxed text-muted-foreground">Approve to admit this applicant to {request.target_tenant.name}, or reject the request.</p></CardHeader>
        <CardContent>
          <form noValidate className="flex flex-col gap-4" onSubmit={form.handleSubmit(({ reason }) => submit('approved', reason))}>
            <div className="flex flex-col gap-2">
              <Label htmlFor={reasonId}>Reason (optional)</Label>
              <textarea id={reasonId} rows={4} maxLength={ADMISSION_REASON_LIMIT} disabled={!canDecide}
                className="w-full min-w-0 resize-y rounded-xl border border-muted-foreground bg-card px-3.5 py-3 text-base text-foreground shadow-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 disabled:opacity-50 sm:text-sm"
                aria-invalid={Boolean(form.formState.errors.reason)} aria-describedby={`${reasonId}-hint${form.formState.errors.reason ? ` ${reasonId}-error` : ''}`} {...form.register('reason')} />
              <p id={`${reasonId}-hint`} className="text-xs text-muted-foreground">Up to 2,000 characters. This reason is saved with the decision.</p>
              {form.formState.errors.reason ? <p id={`${reasonId}-error`} role="alert" className="text-sm text-destructive">{form.formState.errors.reason.message}</p> : null}
            </div>
            <div className="flex flex-wrap gap-3">
              <Button type="submit" className={actionClass} disabled={!canDecide}>{decision.isPending && decision.variables?.decision === 'approved' ? 'Approving...' : 'Approve request'}</Button>
              <Button type="button" variant="outline" className={actionClass} disabled={!canDecide} onClick={form.handleSubmit(({ reason }) => submit('rejected', reason))}>{decision.isPending && decision.variables?.decision === 'rejected' ? 'Rejecting...' : 'Reject request'}</Button>
            </div>
          </form>
        </CardContent>
      </Card> : null}
    </>
  )
}

export function AdmissionApprovalDetailPage() {
  const { id } = useParams()
  const parsedId = admissionIdSchema.safeParse(id)
  const detail = useAdmissionDetail(parsedId.success ? parsedId.data : null)

  return (
    <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
      <header className="flex flex-col gap-3">
        <Link to="/admission-approvals" className="inline-flex min-h-11 items-center self-start rounded-lg py-2 text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring">Back to admission approvals</Link>
        <h1 className="font-display text-2xl font-semibold tracking-tight">Admission request</h1>
      </header>
      {!parsedId.success ? <p role="alert" className="text-sm text-destructive">This admission request link is invalid.</p> : <>
        {detail.isPending ? <Card><CardContent role="status" className="text-sm text-muted-foreground">Loading admission request...</CardContent></Card> : null}
        {detail.isError ? <Card><CardContent className="flex flex-col items-start gap-3">
          <p role="alert" className="text-sm">{detail.error instanceof ApiError ? detail.error.message : 'Unable to load this admission request.'}</p>
          <Button variant="outline" className={actionClass} disabled={detail.isFetching} onClick={() => { void detail.refetch() }}>Retry</Button>
        </CardContent></Card> : null}
        {detail.data ? <AdmissionReview key={detail.data.id} request={detail.data} ready={detail.isSuccess && !detail.isFetching} /> : null}
      </>}
    </div>
  )
}
