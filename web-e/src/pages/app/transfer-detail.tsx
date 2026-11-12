import { useTranslation } from '@/hooks/use-translation'
import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
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
  const { t: translate, owned, locale, formatDate } = useTranslation()
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
      if (isCurrent(scope.owner)) setEntryError(userFacingRequestError(cause, translate("Unable to open the joined workspace. Open the admission below to retry.")))
    })
  }, [joined, enter, isCurrent, scope.owner, translate])
  return <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
    <Button asChild variant="outline"><Link to="/app/notifications?category=admissions">{translate("All admission requests")}</Link></Button>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h1 className="font-display text-2xl font-semibold">{translate("Transfer request")}</h1>
      <Button variant="outline" className="h-auto min-h-11 whitespace-normal" disabled={query.isFetching || withdraw.isPending} onClick={scope.effect(() => { withdraw.reset(); void query.refetch() })}>{translate("Refresh status")}</Button>
    </div>
    {query.isPending ? <p role="status" className="text-sm text-muted-foreground">{translate("Loading transfer request…")}</p> : null}
    {query.isError ? <RequestErrorToast error={query.error} message={userFacingRequestError(query.error, translate("Unable to load this transfer. Refresh to try again."))} /> : null}
    {withdraw.isError ? <RequestErrorToast error={withdraw.error} message={userFacingRequestError(withdraw.error, translate("Unable to withdraw. Refresh the status before trying again."))} /> : null}
    {entryError ? <RequestErrorToast message={entryError} /> : null}
    {request ? <>
      <section aria-label={translate("Transfer status")} className="space-y-3 rounded-xl border border-border p-4">
        <Badge variant="outline">{owned(transferStatuses[request.status].label)}</Badge>
        <p className="text-sm text-muted-foreground">{owned(transferStatuses[request.status].description)}</p>
        {request.choice === 'paid' && request.status === 'admission_created' ? <p className="text-sm text-muted-foreground">{translate("Payment approved. New invitation transfers join the destination automatically without a separate company approval.")}</p> : null}
        {request.review_reason ? <div className="space-y-1"><h2 className="text-sm font-medium">{translate("Review reason")}</h2><p className="whitespace-pre-wrap text-sm">{request.review_reason}</p></div> : null}
        {request.status === 'payment_review' ? <Button variant="outline" className="h-auto min-h-11 whitespace-normal" disabled={withdraw.isPending || query.isFetching || query.isError} onClick={() => withdraw.mutate()}>{withdraw.isPending ? translate("Withdrawing…") : translate("Withdraw transfer request")}</Button> : null}
        {request.admission_request_id ? <Button asChild variant="outline" className="h-auto min-h-11 whitespace-normal"><Link to={`/app/admissions/${encodeURIComponent(request.admission_request_id)}`}>{translate("View destination admission")}</Link></Button> : null}
      </section>
      <section aria-label={translate("Saved transfer policy")} className="space-y-3 rounded-xl border border-border p-4 text-sm">
        <h2 className="font-medium">{translate("Saved fee and payment instructions")}</h2>
        <p>{formatMoney(request.fee_amount_minor, request.currency, locale)} ({request.currency})</p>
        <p className="whitespace-pre-wrap">{request.instructions || translate("No payment instructions were provided. Contact support before paying.")}</p>
        <p className="text-muted-foreground">{translate("This is the policy saved when you submitted, not the current quote. This page does not collect payment or confirm receipt.")}</p>
      </section>
      <dl className="grid gap-4 rounded-xl border border-border p-4 text-sm sm:grid-cols-2">
        <div><dt className="text-muted-foreground">{translate("Choice")}</dt><dd>{request.choice === 'paid' ? translate("Paid · Keep agents and integrations") : translate("Free · Delete agents and integrations on transfer")}</dd></div>
        <div><dt className="text-muted-foreground">{translate("Destination organization ID")}</dt><dd>{request.target_tenant_id}</dd></div>
        <div><dt className="text-muted-foreground">{translate("Source organization ID")}</dt><dd>{request.source_tenant_id}</dd></div>
        <div><dt className="text-muted-foreground">{translate("Submitted")}</dt><dd><time dateTime={request.created_at}>{formatDate(request.created_at, { dateStyle: 'medium', timeStyle: 'short' })}</time></dd></div>
        {request.payment_confirmed_at ? <div><dt className="text-muted-foreground">{translate("Payment confirmed")}</dt><dd><time dateTime={request.payment_confirmed_at}>{formatDate(request.payment_confirmed_at, { dateStyle: 'medium', timeStyle: 'short' })}</time></dd></div> : null}
      </dl>
    </> : null}
  </div>
}
