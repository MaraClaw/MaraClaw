import { useState, type ComponentProps } from 'react'
import { ArrowLeft } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useAuth } from '@/hooks/use-auth'
import { switchTenant } from '@/lib/auth-api'
import { getMyAdmission, withdrawMyAdmission } from '@/lib/company-admissions-api'
import { userFacingRequestError } from '@/lib/http'
import { relativeTime } from '@/lib/relative-time'
import { formatMoney } from '@/lib/format-money'
import type { AdmissionSummary } from '@/lib/types/company-admissions'
import { admissionStatuses } from './admission-labels'

const methods = { invitation: 'Invitation', verified_domain: 'Verified email domain', sso: 'Single sign-on' } satisfies Record<AdmissionSummary['method'], string>
const statusVariants = {
  pending: 'outline',
  approved: 'success',
  rejected: 'destructive',
  withdrawn: 'outline',
} satisfies Record<AdmissionSummary['status'], ComponentProps<typeof Badge>['variant']>

export function AdmissionDetailPage() {
  const { id = '' } = useParams<{ id: string }>()
  return <AdmissionDetail key={id} id={id} />
}

function AdmissionDetail({ id }: { readonly id: string }) {
  const scope = useWorkspaceSession()
  const { user, isCurrent, refreshReplacement } = useAuth()
  const navigate = useNavigate()
  const [opening, setOpening] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const query = useQuery({
    queryKey: ['company-admissions', 'detail', id],
    queryFn: ({ signal }) => getMyAdmission(id, { owner: scope.owner, signal }),
    refetchOnMount: 'always',
  })
  const request = query.data
  const withdraw = useMutation({
    mutationFn: () => withdrawMyAdmission(id, { owner: scope.owner }),
    onMutate() { setError(null) },
    onSuccess(detail) {
      scope.client.setQueryData(['company-admissions', 'detail', id], detail)
      void scope.client.invalidateQueries({ queryKey: ['company-admissions'] })
    },
    onError(cause) {
      setError(userFacingRequestError(cause, 'Unable to withdraw this request. Refresh its status and try again.'))
      void scope.client.invalidateQueries({ queryKey: ['company-admissions'] })
    },
  })

  async function openWorkspace() {
    const expectedOwner = scope.owner
    if (!request || request.status !== 'approved' || !isCurrent(expectedOwner)) return
    setOpening(true)
    setError(null)
    try {
      const result = await switchTenant(request.target_tenant.id, expectedOwner)
      if (!isCurrent(expectedOwner)) return
      const installedOwner = await refreshReplacement(expectedOwner, result.access_token)
      if (!installedOwner || !isCurrent(installedOwner)) return
      navigate('/app', { replace: true })
    } catch (cause) {
      if (isCurrent(expectedOwner)) setError(userFacingRequestError(cause, 'Unable to open the workspace. Try again.'))
    } finally {
      if (isCurrent(expectedOwner)) setOpening(false)
    }
  }

  return (
    <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
      <Button asChild variant="outline"><Link to="/app/admissions"><ArrowLeft aria-hidden="true" className="size-4" />All admission requests</Link></Button>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-semibold">{request?.target_tenant.name ?? 'Admission request'}</h1>
        <Button className="h-auto min-h-11 whitespace-normal" variant="outline" disabled={query.isFetching || opening || withdraw.isPending} onClick={scope.effect(() => { void query.refetch() })}>Refresh status</Button>
      </div>
      {query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading request…</p> : null}
      {query.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load this request. Refresh to try again.')}</p> : null}
      {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
      {request ? (
        <>
          <section aria-label="Admission status" className="space-y-3 rounded-xl border border-border p-4">
            <Badge variant={statusVariants[request.status]}>{admissionStatuses[request.status].label}</Badge>
            <p className="text-sm text-muted-foreground">{admissionStatuses[request.status].description}</p>
            {request.decision_reason ? <div className="space-y-1"><h2 className="text-sm font-medium">Decision reason</h2><p className="whitespace-pre-wrap text-sm">{request.decision_reason}</p></div> : null}
            {request.status === 'pending' ? <Button variant="outline" className="h-auto min-h-11 whitespace-normal"
              disabled={withdraw.isPending || query.isFetching || query.isError} onClick={() => withdraw.mutate()}>
              {withdraw.isPending ? 'Withdrawing request…' : 'Withdraw request'}
            </Button> : null}
            {request.status === 'approved' ? (
              user?.tenant_id === request.target_tenant.id ? <Button asChild><Link to="/app">Open workspace</Link></Button> :
                <Button className="h-auto min-h-11 max-w-full whitespace-normal" disabled={opening || query.isFetching || query.isError} onClick={() => void openWorkspace()}>{opening ? 'Opening workspace…' : `Open ${request.target_tenant.name}`}</Button>
            ) : null}
          </section>
          {request.transfer ? <section aria-label="Transfer choice" className="space-y-2 rounded-xl border border-border p-4 text-sm">
            <h2 className="font-medium">{request.transfer.choice === 'paid' ? 'Paid · Keep agents and integrations' : 'Free · Delete agents and integrations'}</h2>
            {request.transfer.choice === 'paid' ? <p>Saved fee: {formatMoney(request.transfer.fee_amount_minor, request.transfer.currency)} ({request.transfer.currency}). Payment review is separate from this destination approval.</p> : <p>Deletion takes effect only on final destination approval. Agents expire and become unavailable; integrations, credentials, permissions, relationships, schedules, triggers and public pages are deleted. Historical chats, tasks, audit and activity records, accounting, Plaza content, revisions, files and memory remain retained in the source organization, inaccessible to you after the transfer.</p>}
            <Link className="inline-flex min-h-11 items-center font-medium text-primary hover:underline" to={`/app/admissions/transfers/${encodeURIComponent(request.transfer.transfer_request_id)}`}>View saved transfer policy</Link>
          </section> : null}
          <dl className="grid gap-4 rounded-xl border border-border p-4 text-sm sm:grid-cols-2">
            <div><dt className="text-muted-foreground">Applicant</dt><dd>{request.applicant.email ?? request.applicant.id}</dd></div>
            <div><dt className="text-muted-foreground">Source organization</dt><dd>{request.source_tenant?.name ?? 'No organization'}</dd></div>
            <div><dt className="text-muted-foreground">Method</dt><dd>{methods[request.method]}</dd></div>
            <div><dt className="text-muted-foreground">Submitted</dt><dd><time dateTime={request.created_at} title={new Date(request.created_at).toLocaleString()}>{relativeTime(request.created_at)}</time></dd></div>
            {request.decided_at ? <div><dt className="text-muted-foreground">{request.status === 'withdrawn' ? 'Withdrawn' : 'Decided'}</dt><dd><time dateTime={request.decided_at} title={new Date(request.decided_at).toLocaleString()}>{relativeTime(request.decided_at)}</time></dd></div> : null}
          </dl>
        </>
      ) : null}
    </div>
  )
}
