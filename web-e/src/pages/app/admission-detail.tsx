import { useTranslation } from '@/hooks/use-translation'
import { useState, type ComponentProps } from 'react'
import { ArrowLeft } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
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
  const { t: translate, owned, locale, formatDate } = useTranslation()
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
      setError(userFacingRequestError(cause, translate("Unable to withdraw this request. Refresh its status and try again.")))
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
      if (isCurrent(expectedOwner)) setError(userFacingRequestError(cause, translate("Unable to open the workspace. Try again.")))
    } finally {
      if (isCurrent(expectedOwner)) setOpening(false)
    }
  }

  return (
    <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
      <Button asChild variant="outline"><Link to="/app/notifications?category=admissions"><ArrowLeft aria-hidden="true" className="size-4" />{translate("All admission requests")}</Link></Button>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-semibold">{request?.target_tenant.name ?? translate("Admission request")}</h1>
        <Button className="h-auto min-h-11 whitespace-normal" variant="outline" disabled={query.isFetching || opening || withdraw.isPending} onClick={scope.effect(() => { void query.refetch() })}>{translate("Refresh status")}</Button>
      </div>
      {query.isPending ? <p role="status" className="text-sm text-muted-foreground">{translate("Loading request…")}</p> : null}
      {query.isError ? <RequestErrorToast error={query.error} message={userFacingRequestError(query.error, translate("Unable to load this request. Refresh to try again."))} /> : null}
      {error ? <RequestErrorToast message={error} /> : null}
      {request ? (
        <>
          <section aria-label={translate("Admission status")} className="space-y-3 rounded-xl border border-border p-4">
            <Badge variant={statusVariants[request.status]}>{owned(admissionStatuses[request.status].label)}</Badge>
            <p className="text-sm text-muted-foreground">{owned(admissionStatuses[request.status].description)}</p>
            {request.decision_reason ? <div className="space-y-1"><h2 className="text-sm font-medium">{translate("Decision reason")}</h2><p className="whitespace-pre-wrap text-sm">{request.decision_reason}</p></div> : null}
            {request.status === 'pending' ? <Button variant="outline" className="h-auto min-h-11 whitespace-normal"
              disabled={withdraw.isPending || query.isFetching || query.isError} onClick={() => withdraw.mutate()}>
              {withdraw.isPending ? translate("Withdrawing request…") : translate("Withdraw request")}
            </Button> : null}
            {request.status === 'approved' ? (
              user?.tenant_id === request.target_tenant.id ? <Button asChild><Link to="/app">{translate("Open workspace")}</Link></Button> :
                <Button className="h-auto min-h-11 max-w-full whitespace-normal" disabled={opening || query.isFetching || query.isError} onClick={() => void openWorkspace()}>{opening ? translate("Opening workspace…") : translate("Open {organization}", { organization: request.target_tenant.name })}</Button>
            ) : null}
          </section>
          {request.transfer ? <section aria-label={translate("Transfer choice")} className="space-y-2 rounded-xl border border-border p-4 text-sm">
            <h2 className="font-medium">{request.transfer.choice === 'paid' ? translate("Paid · Keep agents and integrations") : translate("Free · Delete agents and integrations")}</h2>
            {request.transfer.choice === 'paid' ? <p>{translate("Saved fee:")}{' '}{formatMoney(request.transfer.fee_amount_minor, request.transfer.currency, locale)} ({request.transfer.currency}{translate("). New paid transfers join automatically after payment approval.")}</p> : <p>{translate("Deletion takes effect when the transfer completes. Agents expire and become unavailable; integrations, credentials, permissions, relationships, schedules, triggers and public pages are deleted. Historical chats, tasks, audit and activity records, accounting, Clawbook content, revisions, files and memory remain retained in the source organization, inaccessible to you after the transfer.")}</p>}
            <Link className="inline-flex min-h-11 items-center font-medium text-primary hover:underline" to={`/app/admissions/transfers/${encodeURIComponent(request.transfer.transfer_request_id)}`}>{translate("View saved transfer policy")}</Link>
          </section> : null}
          <dl className="grid gap-4 rounded-xl border border-border p-4 text-sm sm:grid-cols-2">
            <div><dt className="text-muted-foreground">{translate("Applicant")}</dt><dd>{request.applicant.email ?? request.applicant.id}</dd></div>
            <div><dt className="text-muted-foreground">{translate("Source organization")}</dt><dd>{request.source_tenant?.name ?? translate("No organization")}</dd></div>
            <div><dt className="text-muted-foreground">{translate("Method")}</dt><dd>{owned(methods[request.method])}</dd></div>
            <div><dt className="text-muted-foreground">{translate("Submitted")}</dt><dd><time dateTime={request.created_at} title={formatDate(request.created_at, { dateStyle: 'medium', timeStyle: 'short' })}>{relativeTime(request.created_at, undefined, locale)}</time></dd></div>
            {request.decided_at ? <div><dt className="text-muted-foreground">{request.status === 'withdrawn' ? translate("Withdrawn") : translate("Decided")}</dt><dd><time dateTime={request.decided_at} title={formatDate(request.decided_at, { dateStyle: 'medium', timeStyle: 'short' })}>{relativeTime(request.decided_at, undefined, locale)}</time></dd></div> : null}
          </dl>
        </>
      ) : null}
    </div>
  )
}
