import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useAuth } from '@/hooks/use-auth'
import { useJoinedWorkspace } from '@/hooks/use-joined-workspace'
import { getMyAdmission } from '@/lib/company-admissions-api'
import { getMyTransfer, withdrawMyTransfer } from '@/lib/company-transfers-api'
import { formatMoney } from '@/lib/format-money'
import { userFacingRequestError } from '@/lib/http'
import { transferStatuses } from './transfer-labels'

export function TransferDetailPage() {
  const { id = '' } = useParams<{ id: string }>()
  return <TransferDetail key={id} id={id} />
}

function TransferDetail({ id }: { readonly id: string }) {
  const scope = useWorkspaceSession()
  const { isCurrent } = useAuth()
  const enter = useJoinedWorkspace()
  const attempted = useRef(false)
  const [entryError, setEntryError] = useState<string | null>(null)
  const query = useQuery({
    queryKey: ['company-transfers', 'detail', id],
    queryFn: ({ signal }) => getMyTransfer(id, { owner: scope.owner, signal }),
    refetchOnMount: 'always', retry: false,
  })
  const withdraw = useMutation({
    mutationFn: () => withdrawMyTransfer(id, { owner: scope.owner }),
    onSuccess(detail) {
      scope.client.setQueryData(['company-transfers', 'detail', id], detail)
      void scope.client.invalidateQueries({ queryKey: ['company-transfers'] })
    },
    onError() { void scope.client.invalidateQueries({ queryKey: ['company-transfers'] }) },
  })
  const request = query.data
  const admissionId = request?.admission_request_id
  const destination = useQuery({
    queryKey: ['company-admissions', 'detail', admissionId],
    queryFn: ({ signal }) => getMyAdmission(admissionId ?? '', { owner: scope.owner, signal }),
    enabled: request?.status === 'admission_created' && Boolean(admissionId), retry: false,
  })
  const joined = destination.data
  useEffect(() => {
    if (joined?.status !== 'approved' || attempted.current || !isCurrent(scope.owner)) return
    attempted.current = true
    void enter({ status: 'approved', request: { ...joined, status: 'approved' } }).catch((cause: unknown) => {
      if (isCurrent(scope.owner)) setEntryError(userFacingRequestError(cause, 'Unable to open the joined workspace. Open the admission below to retry.'))
    })
  }, [joined, enter, isCurrent, scope.owner])
  return <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
    <Button asChild variant="outline"><Link to="/app/admissions">All admission requests</Link></Button>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h1 className="font-display text-2xl font-semibold">Transfer request</h1>
      <Button variant="outline" className="h-auto min-h-11 whitespace-normal" disabled={query.isFetching || withdraw.isPending} onClick={scope.effect(() => { withdraw.reset(); void query.refetch() })}>Refresh status</Button>
    </div>
    {query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading transfer request…</p> : null}
    {query.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load this transfer. Refresh to try again.')}</p> : null}
    {withdraw.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(withdraw.error, 'Unable to withdraw. Refresh the status before trying again.')}</p> : null}
    {entryError ? <p role="alert" className="text-sm text-destructive">{entryError}</p> : null}
    {request ? <>
      <section aria-label="Transfer status" className="space-y-3 rounded-xl border border-border p-4">
        <Badge variant="outline">{transferStatuses[request.status].label}</Badge>
        <p className="text-sm text-muted-foreground">{transferStatuses[request.status].description}</p>
        {request.choice === 'paid' && request.status === 'admission_created' ? <p className="text-sm text-muted-foreground">Payment approved. New invitation transfers join the destination automatically without a separate company approval.</p> : null}
        {request.review_reason ? <div className="space-y-1"><h2 className="text-sm font-medium">Review reason</h2><p className="whitespace-pre-wrap text-sm">{request.review_reason}</p></div> : null}
        {request.status === 'payment_review' ? <Button variant="outline" className="h-auto min-h-11 whitespace-normal" disabled={withdraw.isPending || query.isFetching || query.isError} onClick={() => withdraw.mutate()}>{withdraw.isPending ? 'Withdrawing…' : 'Withdraw transfer request'}</Button> : null}
        {request.admission_request_id ? <Button asChild variant="outline" className="h-auto min-h-11 whitespace-normal"><Link to={`/app/admissions/${encodeURIComponent(request.admission_request_id)}`}>View destination admission</Link></Button> : null}
      </section>
      <section aria-label="Saved transfer policy" className="space-y-3 rounded-xl border border-border p-4 text-sm">
        <h2 className="font-medium">Saved fee and payment instructions</h2>
        <p>{formatMoney(request.fee_amount_minor, request.currency)} ({request.currency})</p>
        <p className="whitespace-pre-wrap">{request.instructions || 'No payment instructions were provided. Contact support before paying.'}</p>
        <p className="text-muted-foreground">This is the policy saved when you submitted, not the current quote. This page does not collect payment or confirm receipt.</p>
      </section>
      <dl className="grid gap-4 rounded-xl border border-border p-4 text-sm sm:grid-cols-2">
        <div><dt className="text-muted-foreground">Choice</dt><dd>{request.choice === 'paid' ? 'Paid · Keep agents and integrations' : 'Free · Delete agents and integrations on transfer'}</dd></div>
        <div><dt className="text-muted-foreground">Destination organization ID</dt><dd>{request.target_tenant_id}</dd></div>
        <div><dt className="text-muted-foreground">Source organization ID</dt><dd>{request.source_tenant_id}</dd></div>
        <div><dt className="text-muted-foreground">Submitted</dt><dd><time dateTime={request.created_at}>{new Date(request.created_at).toLocaleString()}</time></dd></div>
        {request.payment_confirmed_at ? <div><dt className="text-muted-foreground">Payment confirmed</dt><dd><time dateTime={request.payment_confirmed_at}>{new Date(request.payment_confirmed_at).toLocaleString()}</time></dd></div> : null}
      </dl>
    </> : null}
  </div>
}
