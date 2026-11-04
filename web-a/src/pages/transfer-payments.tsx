import { formatDistanceToNowStrict } from 'date-fns'
import { useQueryClient } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { transferReviewKeys } from '@/hooks/transfer-review-options'
import { useTransferReviews } from '@/hooks/use-transfer-reviews'
import { ApiError } from '@/lib/http'
import { transferReviewViewSchema, type TransferReviewView } from '@/lib/transfer-reviews-api'
import { transferActionClass, transferFeeLabel, transferStatusLabels, transferStatusVariants } from './transfer-payment-labels'

export function TransferPaymentsPage() {
  const filterId = useId()
  const client = useQueryClient()
  const [view, setView] = useState<TransferReviewView>('pending')
  const [cursors, setCursors] = useState<readonly string[]>([])
  const reviews = useTransferReviews(view, cursors.at(-1) ?? null)
  const items = reviews.data?.items ?? []
  const nextCursor = reviews.data?.next_cursor ?? null
  const pendingOnly = view === 'pending'

  function refresh() {
    setCursors([])
    void client.invalidateQueries({ queryKey: transferReviewKeys.list(view, null), exact: true })
  }

  return <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <header>
      <h1 className="font-display text-2xl font-semibold tracking-tight">Transfer payments</h1>
      <p className="mt-1 text-sm leading-relaxed text-muted-foreground">Check payments for requests to leave the default organization. Approval creates a destination admission request; it does not move the member.</p>
    </header>
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="flex flex-col gap-2">
        <Label htmlFor={filterId}>Payment requests</Label>
        <Select id={filterId} fit value={view} onChange={(event) => {
          setView(transferReviewViewSchema.parse(event.currentTarget.value))
          setCursors([])
        }}>
          <option value="pending">Pending payment review</option><option value="recent">Recent paid-choice requests</option>
        </Select>
      </div>
      <Button variant="outline" className={transferActionClass} disabled={reviews.isFetching} onClick={refresh}>Refresh payments</Button>
    </div>
    {reviews.isPending ? <Card><CardContent role="status" className="text-sm text-muted-foreground">Loading transfer payments...</CardContent></Card> : null}
    {reviews.isError ? <Card><CardContent className="flex flex-col items-start gap-3">
      <p role="alert" className="text-sm">{reviews.error instanceof ApiError ? reviews.error.message : 'Unable to load transfer payments. Check your connection and retry.'}</p>
      <Button variant="outline" className={transferActionClass} disabled={reviews.isFetching} onClick={() => { void reviews.refetch() }}>Retry</Button>
    </CardContent></Card> : null}
    {reviews.isSuccess && items.length === 0 ? <Card><CardContent className="flex flex-col gap-2 py-4">
      <h2 className="font-display text-lg font-semibold">{pendingOnly ? 'No pending payments' : 'No paid-choice requests'}</h2>
      <p className="text-sm text-muted-foreground">{cursors.length > 0 ? 'Return to the previous page to see earlier requests.' : `Refresh to check for new requests${pendingOnly ? ', or select Recent paid-choice requests to inspect reviewed payments.' : '.'}`}</p>
    </CardContent></Card> : null}
    {items.length > 0 ? <ul aria-label="Transfer payment requests" className="flex flex-col gap-4">{items.map((request) => <li key={request.id}>
      <Card><CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Badge variant={transferStatusVariants[request.status]}>{transferStatusLabels[request.status]}</Badge>
          <time dateTime={request.created_at} title={new Date(request.created_at).toLocaleString()} className="text-xs text-muted-foreground">{formatDistanceToNowStrict(new Date(request.created_at), { addSuffix: true })}</time>
        </div>
        <h2 className="font-display text-base font-semibold">Applicant ID: {request.applicant_user_id}</h2>
        <p className="text-sm text-muted-foreground">Destination organization ID: {request.target_tenant_id}</p>
        <p className="text-sm">Snapshot fee: {transferFeeLabel(request)}</p>
        <Button asChild variant="outline" className={`self-start ${transferActionClass}`}><Link to={`/transfer-payments/${request.id}`} aria-label={`Inspect payment request ${request.id}`}>Inspect payment</Link></Button>
      </CardContent></Card>
    </li>)}</ul> : null}
    {cursors.length > 0 || items.length > 0 || nextCursor !== null ? <nav aria-label="Transfer payment pages" className="flex flex-wrap items-center justify-between gap-3">
      <Button variant="outline" className={transferActionClass} disabled={cursors.length === 0 || reviews.isFetching} onClick={() => setCursors((value) => value.slice(0, -1))}>Previous</Button>
      <p role="status" className="text-xs text-muted-foreground">Page {cursors.length + 1}</p>
      <Button variant="outline" className={transferActionClass} disabled={nextCursor === null || reviews.isFetching || reviews.isError} onClick={() => { if (nextCursor !== null) setCursors((value) => [...value, nextCursor]) }}>Next</Button>
    </nav> : null}
  </div>
}
