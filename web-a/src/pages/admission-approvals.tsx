import { formatDistanceToNowStrict } from 'date-fns'
import { useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { admissionKeys } from '@/hooks/admission-options'
import { useAdmissionReviews } from '@/hooks/use-admissions'
import { admissionStatusSchema, type AdmissionStatus } from '@/lib/admissions-api'
import { ApiError } from '@/lib/http'
import { admissionMethodLabels, admissionStatusLabels } from './admission-labels'

const actionClass = 'h-auto min-h-11 whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

export function AdmissionApprovalsPage() {
  const filterId = useId()
  const client = useQueryClient()
  const [status, setStatus] = useState<AdmissionStatus>('pending')
  const [cursors, setCursors] = useState<readonly string[]>([])
  const reviews = useAdmissionReviews(status, cursors.at(-1) ?? null)
  const items = reviews.data?.items ?? []
  const nextCursor = reviews.data?.next_cursor ?? null

  function refresh() {
    setCursors([])
    void client.invalidateQueries({ queryKey: admissionKeys.list(status, null), exact: true })
  }

  return (
    <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
      <header>
        <h1 className="font-display text-2xl font-semibold tracking-tight">Admission approvals</h1>
        <p className="mt-1 text-sm leading-relaxed text-muted-foreground">Review requests to join your organization.</p>
      </header>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-2">
          <Label htmlFor={filterId}>Request status</Label>
          <Select id={filterId} fit value={status} onChange={(event) => {
            setStatus(admissionStatusSchema.parse(event.currentTarget.value))
            setCursors([])
          }}>
            {admissionStatusSchema.options.map((value) => <option key={value} value={value}>{admissionStatusLabels[value]}</option>)}
          </Select>
        </div>
        <Button variant="outline" className={actionClass} disabled={reviews.isFetching} onClick={refresh}>Refresh requests</Button>
      </div>
      {reviews.isPending ? <Card><CardContent role="status" className="text-sm text-muted-foreground">Loading admission requests...</CardContent></Card> : null}
      {reviews.isError ? <Card><CardContent className="flex flex-col items-start gap-3">
        <p role="alert" className="text-sm">{reviews.error instanceof ApiError ? reviews.error.message : 'Unable to load admission requests.'}</p>
        <Button variant="outline" className={actionClass} disabled={reviews.isFetching} onClick={() => { void reviews.refetch() }}>Retry</Button>
      </CardContent></Card> : null}
      {reviews.isSuccess && items.length === 0 ? <Card><CardContent className="flex flex-col gap-2 py-4">
        <h2 className="font-display text-lg font-semibold">No {admissionStatusLabels[status].toLowerCase()} requests</h2>
        <p className="text-sm text-muted-foreground">{cursors.length === 0 ? 'Requests will appear here when someone asks to join your organization.' : 'Return to the previous page to see earlier requests.'}</p>
      </CardContent></Card> : null}
      {items.length > 0 ? <ul aria-label="Admission requests" className="flex flex-col gap-4">
        {items.map((request) => <li key={request.id}>
          <Card><CardContent className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Badge variant={request.status === 'pending' ? 'warning' : 'secondary'}>{admissionStatusLabels[request.status]}</Badge>
              <time className="text-xs text-muted-foreground" dateTime={request.created_at} title={new Date(request.created_at).toLocaleString()}>{formatDistanceToNowStrict(new Date(request.created_at), { addSuffix: true })}</time>
            </div>
            <h2 className="font-display text-base font-semibold">{request.applicant.email ?? 'Applicant'}</h2>
            <p className="text-sm text-muted-foreground">{request.source_tenant?.name ?? 'No source organization'} → {request.target_tenant.name}</p>
            <p className="text-sm text-muted-foreground">{admissionMethodLabels[request.method]}</p>
            <Button asChild variant="outline" className={`self-start ${actionClass}`}>
              <Link to={`/admission-approvals/${request.id}`} aria-label={`Review request from ${request.applicant.email ?? 'applicant'}`}>Review request</Link>
            </Button>
          </CardContent></Card>
        </li>)}
      </ul> : null}
      {cursors.length > 0 || items.length > 0 || nextCursor !== null ? <nav aria-label="Admission request pages" className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" className={actionClass} disabled={cursors.length === 0 || reviews.isFetching} onClick={() => setCursors((value) => value.slice(0, -1))}>Previous</Button>
        <p role="status" className="text-xs text-muted-foreground">Page {cursors.length + 1}</p>
        <Button variant="outline" className={actionClass} disabled={nextCursor === null || reviews.isFetching || reviews.isError} onClick={() => { if (nextCursor !== null) setCursors((value) => [...value, nextCursor]) }}>Next</Button>
      </nav> : null}
    </div>
  )
}
