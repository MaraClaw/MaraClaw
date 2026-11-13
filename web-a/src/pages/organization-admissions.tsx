import { zodResolver } from '@hookform/resolvers/zod'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { useTranslation } from '@/hooks/use-translation'

import { Badge } from '@/components/ui/badge'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { organizationAdmissionKeys } from '@/hooks/organization-admission-options'
import { useOrganizationAdmissionDecision, useOrganizationAdmissionEmailRetry, useOrganizationAdmissions } from '@/hooks/use-organization-admissions'
import { ApiError } from '@/lib/http'
import {
  ORGANIZATION_ADMISSIONS_PAGE_SIZE, ORGANIZATION_ADMISSION_NOTE_LIMIT,
  organizationAdmissionNoteSchema, organizationAdmissionStatusSchema,
  type OrganizationAdmission, type OrganizationAdmissionDecision, type OrganizationAdmissionStatus,
} from '@/lib/organization-admissions-api'

const statusLabels = { pending: 'Pending', approved: 'Approved', rejected: 'Rejected' } as const
const actionClass = 'h-auto min-h-11 whitespace-normal'
const noteDraftSchema = z.object({ note: organizationAdmissionNoteSchema })
const credentialEmailLabels = {
  pending: 'Initial credentials email is queued for delivery.',
  sent: 'The SMTP server accepted the initial credentials email. This does not guarantee inbox delivery.',
  failed: 'Initial credentials email delivery failed.',
} as const

function OrganizationAdmissionReview({ request, ready, onBack }: {
  readonly request: OrganizationAdmission; readonly ready: boolean; readonly onBack: () => void
}) {
  const { t, date, number, owned } = useTranslation()
  const usd = (amount: number) => number(amount, { style: 'currency', currency: 'USD' })
  const noteId = useId()
  const submitting = useRef(false)
  const retryingEmail = useRef(false)
  const resultHeading = useRef<HTMLHeadingElement>(null)
  const [confirmed, setConfirmed] = useState(false)
  useEffect(() => { resultHeading.current?.focus() }, [])
  const decision = useOrganizationAdmissionDecision(request.id)
  const emailRetry = useOrganizationAdmissionEmailRetry(request.id)
  const form = useForm<z.infer<typeof noteDraftSchema>>({ resolver: zodResolver(noteDraftSchema), defaultValues: { note: '' } })
  const detail = emailRetry.data ?? decision.data ?? request
  const busy = decision.isPending || emailRetry.isPending
  const conflict = decision.error instanceof ApiError && decision.error.status === 409
  const canDecide = ready && detail.status === 'pending' && !decision.isPending && !conflict
  const canApprove = canDecide && detail.organization_name !== null
  const fields = [
    ['Requested tier', detail.tier.toUpperCase()], ['Quoted plan', detail.plan.name],
    ['Plan ID', detail.plan.id], ['Currency', detail.plan.currency],
    ['Monthly price', usd(detail.plan.monthly_price_usd)], ['Included accounts', number(detail.plan.included_accounts)],
    ['Additional account / month', usd(detail.plan.additional_account_monthly_usd)],
    ['Maximum accounts', number(detail.plan.max_accounts)],
    ['Maximum agents per account', number(detail.plan.max_agents_per_account)],
    ['Daily messages per agent', number(detail.plan.daily_messages_per_agent)],
    ['Managed S3 storage per agent', `${number(detail.plan.storage_limit_bytes / (1024 ** 3))} GiB`],
    ['Maximum managed S3 file size', `${number(detail.plan.max_file_size_bytes / (1024 ** 2))} MiB`],
    ['Requested', date(detail.created_at)],
    ['Reviewed', detail.reviewed_at ? date(detail.reviewed_at) : t('Not reviewed')],
    ['Reviewed by', detail.reviewed_by ?? t('Not reviewed')],
  ] as const

  function submit(value: OrganizationAdmissionDecision['decision'], note: string) {
    if (!canDecide || (value === 'approve' && (!canApprove || !confirmed)) || submitting.current) return
    submitting.current = true
    decision.mutate({ decision: value, ...(note ? { note } : {}) }, {
      onSuccess: () => { resultHeading.current?.focus() },
      onSettled: () => { submitting.current = false; setConfirmed(false) },
    })
  }

  return <>
    <Button variant="outline" className={`self-start ${actionClass}`} disabled={busy} onClick={onBack}>{t('Back to requests')}</Button>
    <Card>
      <CardHeader className="gap-3">
        <h2 ref={resultHeading} tabIndex={-1} className="font-display text-lg font-semibold outline-none">{detail.organization_name ?? t('Organization name not provided')}</h2>
        <p className="text-sm">{detail.email}</p>
        <Badge variant="secondary">{t(statusLabels[detail.status])}</Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <dl className="grid gap-4 sm:grid-cols-2">
          {fields.map(([label, value]) => <div key={label} className="min-w-0">
            <dt className="text-xs text-muted-foreground">{t(label)}</dt><dd className="mt-1 text-sm tabular-nums">{value}</dd>
          </div>)}
        </dl>
        <p className="max-w-prose text-sm leading-relaxed text-muted-foreground">{t('Storage allowances apply to managed S3 uploads/files only. Direct guest workspace/local writes are excluded. Some upload tools have stricter limits.')}</p>
        {detail.review_note !== null ? <div><h3 className="font-medium">{t('Saved review note')}</h3><p className="mt-2 whitespace-pre-wrap text-sm">{detail.review_note || t('No note provided.')}</p></div> : null}
        <p className="max-w-prose text-sm leading-relaxed text-muted-foreground">{t('Approval creates the named organization and its genesis administrator account, then queues an email with initial sign-in credentials to the applicant. The administrator must change the password at first sign-in. Payment is arranged separately.')}</p>
        {detail.status === 'approved' && detail.credential_email_status !== null ? <section aria-labelledby={`${noteId}-email`} className="flex flex-col items-start gap-3">
          <h3 id={`${noteId}-email`} className="font-medium">{t('Initial credentials email')}</h3>
          <p role="status" className="text-sm">{t(credentialEmailLabels[detail.credential_email_status])}</p>
          {detail.credential_email_status === 'failed' ? <>
            {detail.credential_email_error !== null ? <p className="text-sm">{t('Delivery error:')} {detail.credential_email_error}</p> : null}
            <p id={`${noteId}-retry-hint`} className="text-sm text-muted-foreground">{t('Retry queues the same credentials. It does not create another organization or change the password. If the administrator has already changed the password, the original credentials will not be resent.')}</p>
            <Button type="button" variant="outline" className={actionClass} disabled={busy} aria-describedby={`${noteId}-retry-hint`} onClick={() => {
              if (busy || retryingEmail.current) return
              retryingEmail.current = true
              emailRetry.mutate(undefined, { onSettled: () => { retryingEmail.current = false } })
            }}>{t('Retry credentials email')}</Button>
          </> : null}
          <p role="status" className="text-sm">{emailRetry.isPending ? t('Queueing credentials email...') : ''}</p>
        {emailRetry.isError ? <ErrorToast error={emailRetry.error} message={emailRetry.error instanceof ApiError ? emailRetry.error.message : t('Unable to queue the credentials email. Try again.')} /> : null}
          <p className="text-xs text-muted-foreground">{t('Return to requests, refresh, and reopen this request to see the latest delivery status.')}</p>
        </section> : null}
        {detail.status === 'pending' && detail.organization_name === null ? <p id={`${noteId}-organization`} className="text-sm">{t('This request cannot be approved without an organization name. Reject it and ask the applicant to resubmit with an organization name.')}</p> : null}
        <p role="status" className="text-sm">{decision.isPending ? t('Saving decision...') : decision.isSuccess ? t('Request {status}.', { status: t(statusLabels[detail.status]).toLowerCase() }) : ''}</p>
    {decision.isError ? <div className="flex flex-col gap-2"><ErrorToast error={decision.error} message={decision.error instanceof ApiError ? decision.error.message : t('Unable to save your decision. Try again.')} />{conflict ? <p className="text-sm">{t('Return to requests and refresh before reviewing again.')}</p> : null}</div> : null}
        {detail.status === 'pending' ? <form noValidate className="flex flex-col gap-4" onSubmit={form.handleSubmit(({ note }) => submit('approve', note))}>
          <div className="flex flex-col gap-2">
            <Label htmlFor={noteId}>{t('Review note (optional)')}</Label>
            <textarea id={noteId} rows={4} maxLength={ORGANIZATION_ADMISSION_NOTE_LIMIT} disabled={!canDecide}
              className="w-full min-w-0 resize-y rounded-xl border border-muted-foreground bg-card px-3 py-3 text-base text-foreground caret-primary outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 sm:text-sm"
              aria-invalid={Boolean(form.formState.errors.note)} aria-describedby={`${noteId}-hint${form.formState.errors.note ? ` ${noteId}-error` : ''}`} {...form.register('note')} />
            <p id={`${noteId}-hint`} className="text-xs text-muted-foreground">{t('Up to 1,000 characters, saved with the decision.')}</p>
            {form.formState.errors.note ? <p id={`${noteId}-error`} role="alert" className="text-sm text-destructive">{owned(form.formState.errors.note.message)}</p> : null}
          </div>
          <label htmlFor={`${noteId}-confirm`} className="flex min-h-11 cursor-pointer items-start gap-3 py-2 text-sm leading-relaxed">
            <input id={`${noteId}-confirm`} type="checkbox" checked={confirmed} disabled={!canDecide} onChange={(event) => setConfirmed(event.currentTarget.checked)} className="mt-1 size-5 shrink-0 accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-50" />
            {t('I understand that approval creates the organization and its genesis administrator account and queues their initial sign-in credentials by email, and that a saved decision cannot be changed.')}
          </label>
          <div className="flex flex-wrap gap-3">
            <Button type="submit" className={actionClass} disabled={!canApprove || !confirmed} aria-describedby={detail.organization_name === null ? `${noteId}-organization` : undefined}>{t('Approve request')}</Button>
            <Button type="button" variant="outline" className={`${actionClass} text-destructive hover:text-destructive`} disabled={!canDecide} onClick={form.handleSubmit(({ note }) => submit('reject', note))}>{t('Reject request')}</Button>
          </div>
        </form> : null}
      </CardContent>
    </Card>
  </>
}

export function OrganizationAdmissionsPage() {
  const { t, date, number } = useTranslation()
  const filterId = useId()
  const queueHeading = useRef<HTMLHeadingElement>(null)
  const client = useQueryClient()
  const [status, setStatus] = useState<OrganizationAdmissionStatus>('pending')
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<OrganizationAdmission | null>(null)
  useEffect(() => { if (selected === null) queueHeading.current?.focus() }, [selected])
  const reviews = useOrganizationAdmissions(status, offset)
  const items = reviews.data?.items ?? []

  return <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <header><h1 ref={queueHeading} tabIndex={-1} className="font-display text-2xl font-semibold tracking-tight outline-none">{t('Organization admissions')}</h1><p className="mt-2 text-sm text-muted-foreground">{t('Review requests for a new organization and the commercial terms quoted at submission.')}</p></header>
    {selected && reviews.isFetching ? <p role="status" className="text-sm">{t('Refreshing request status...')}</p> : null}
    {reviews.isError ? <div className="flex flex-col items-start gap-3"><ErrorToast error={reviews.error} message={reviews.error instanceof ApiError ? reviews.error.message : t('Unable to load organization requests.')} /><Button variant="outline" disabled={reviews.isFetching} onClick={() => { void reviews.refetch() }}>{t('Retry')}</Button></div> : null}
    {selected ? <OrganizationAdmissionReview key={selected.id} request={items.find((item) => item.id === selected.id) ?? selected} ready={reviews.isSuccess && !reviews.isFetching && items.some((item) => item.id === selected.id)} onBack={() => setSelected(null)} /> : <>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-2"><Label htmlFor={filterId}>{t('Request status')}</Label>
          <Select id={filterId} fit value={status} onChange={(event) => { setStatus(organizationAdmissionStatusSchema.parse(event.currentTarget.value)); setOffset(0) }}>
            {organizationAdmissionStatusSchema.options.map((value) => <option key={value} value={value}>{t(statusLabels[value])}</option>)}
          </Select>
        </div>
        <Button variant="outline" className={actionClass} disabled={reviews.isFetching} onClick={() => { setOffset(0); void client.invalidateQueries({ queryKey: organizationAdmissionKeys.all }) }}>{t('Refresh requests')}</Button>
      </div>
      {reviews.isPending ? <p role="status" className="text-sm">{t('Loading organization requests...')}</p> : null}
      {reviews.isSuccess && items.length === 0 ? <p role="status" className="text-sm">{t('No {status} requests', { status: t(statusLabels[status]).toLowerCase() })}. {t(offset > 0 ? 'Return to the previous page.' : 'New requests will appear here after submission.')}</p> : null}
      {reviews.isSuccess && items.length > 0 ? <ul aria-label={t('Organization admission requests')} className="flex flex-col gap-4">
        {items.map((request) => <li key={request.id}><Card><CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-display text-lg font-semibold">{request.organization_name ?? t('Organization name not provided')}</h2><Badge variant="secondary">{t(statusLabels[request.status])}</Badge></div>
          <p className="text-sm">{request.email}</p>
          <p className="text-sm">{request.tier.toUpperCase()} / {request.plan.name} / <span className="tabular-nums">{number(request.plan.monthly_price_usd, { style: 'currency', currency: 'USD' })}</span> {t('per month')}</p>
          <time dateTime={request.created_at} className="text-xs text-muted-foreground">{date(request.created_at)}</time>
          <Button variant="outline" className={`self-start ${actionClass}`} onClick={() => setSelected(request)} aria-label={t('Review request from {email}', { email: request.email })}>{t('Review request')}</Button>
        </CardContent></Card></li>)}
      </ul> : null}
      <nav aria-label={t('Organization admission pages')} className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" className={actionClass} disabled={offset === 0 || reviews.isFetching} onClick={() => setOffset((value) => Math.max(0, value - ORGANIZATION_ADMISSIONS_PAGE_SIZE))}>{t('Previous')}</Button>
        <p role="status" className="text-xs tabular-nums">{t('Page {page}', { page: number(offset / ORGANIZATION_ADMISSIONS_PAGE_SIZE + 1) })}</p>
        <Button variant="outline" className={actionClass} disabled={!reviews.data?.has_more || !reviews.isSuccess || reviews.isFetching} onClick={() => setOffset((value) => value + ORGANIZATION_ADMISSIONS_PAGE_SIZE)}>{t('Next')}</Button>
      </nav>
    </>}
  </div>
}
