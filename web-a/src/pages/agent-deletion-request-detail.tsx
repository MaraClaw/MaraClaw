import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { useAgentDeletionApproval, useAgentDeletionDetail } from '@/hooks/use-agent-deletions'
import { agentDeletionIdSchema, canApproveAgentDeletion, type AgentDeletionDetail } from '@/lib/agent-deletion-api'
import { agentLifecycleError } from '@/lib/agent-lifecycle-errors'

const cancellationLabels = {
  withdrawn: 'Withdrawn by requester', owner_changed: 'Agent ownership changed',
  tenant_changed: 'Organization changed', agent_gone: 'Agent no longer exists',
} as const

function DeletionReview({ request, ready }: { readonly request: AgentDeletionDetail; readonly ready: boolean }) {
  const approval = useAgentDeletionApproval(request.id)
  const submitting = useRef(false)
  const [confirmed, setConfirmed] = useState(false)
  const canApprove = canApproveAgentDeletion(request, ready) && !approval.isPending
  const fields = [
    ['Agent ID', request.agent_id],
    ['Requester', request.requester.display_name ?? request.requester.id],
    ['Requester ID', request.requester.id],
    ['Requested', new Date(request.created_at).toLocaleString()],
    ['Resolved', request.resolved_at ? new Date(request.resolved_at).toLocaleString() : 'Not resolved'],
    ['Cancellation reason', request.cancel_reason ? cancellationLabels[request.cancel_reason] : 'None'],
  ] as const

  return <>
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
        <h2 className="font-display text-lg font-semibold">{request.agent_name}</h2><Badge variant="secondary">{request.status}</Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div><h3 className="text-sm font-medium">Deletion reason</h3><p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">{request.reason}</p></div>
        <dl className="grid gap-4 sm:grid-cols-2">{fields.map(([label, value]) => <div key={label} className="min-w-0">
          <dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-1 text-sm">{value}</dd>
        </div>)}</dl>
      </CardContent>
    </Card>
    <Card><CardHeader><h2 className="font-display text-lg font-semibold">Agent metadata</h2></CardHeader><CardContent>
      {request.agent ? <dl className="grid gap-4 text-sm sm:grid-cols-2">
        {([
          ['Name', request.agent.name], ['Role description', request.agent.role_description ?? 'Not provided'],
          ['Creator', request.agent.creator.display_name ?? request.agent.creator.id], ['Creator ID', request.agent.creator.id],
          ['Access mode', request.agent.access_mode], ['Status', request.agent.status],
          ['Private agent', request.agent.is_private ? 'Yes' : 'No'],
          ['Last active', request.agent.last_active_at ? new Date(request.agent.last_active_at).toLocaleString() : 'No activity recorded'],
        ] as const).map(([label, value]) => <div key={label} className="min-w-0"><dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-1 whitespace-pre-wrap">{value}</dd></div>)}
      </dl> : <p className="text-sm text-muted-foreground">This agent no longer exists.</p>}
    </CardContent></Card>
    <Card><CardHeader><h2 className="font-display text-lg font-semibold">Approval progress</h2></CardHeader><CardContent className="flex flex-col gap-4">
      <p className="text-sm">Deletion requires {request.progress.required_approvals} counted org-admin approvals, or approval from the genesis org admin.</p>
      <p className="text-sm font-medium">{request.progress.counted_approvals} / {request.progress.required_approvals} counted approvals · Genesis {request.progress.genesis_approved ? 'approved' : 'not approved'}</p>
      {request.approvals.length === 0 ? <p className="text-sm text-muted-foreground">No approvals yet.</p> : <ul className="divide-y divide-border" aria-label="Deletion approvals">
        {request.approvals.map((reviewer) => <li key={reviewer.reviewer_id} className="flex flex-col gap-1 py-3 text-sm">
          <p className="font-medium">{reviewer.display_name ?? reviewer.reviewer_id}{reviewer.is_genesis ? ' · Genesis org admin' : ''}</p>
          <p>{reviewer.counted ? 'Counted approval' : 'Not counted toward the approval requirement'}</p>
          <time className="text-xs text-muted-foreground" dateTime={reviewer.approved_at}>{new Date(reviewer.approved_at).toLocaleString()}</time>
        </li>)}
      </ul>}
    </CardContent></Card>
    <p role="status" className="text-sm">{approval.isSuccess ? 'Your approval was recorded.' : request.viewer.has_approved ? 'You have already approved this request.' : ''}</p>
    {approval.isError ? <p role="alert" className="text-sm text-destructive">{agentLifecycleError(approval.error)}</p> : null}
    {request.status === 'pending' ? <Card><CardHeader><h2 className="font-display text-lg font-semibold">Approve deletion</h2></CardHeader><CardContent className="flex flex-col items-start gap-4">
      <p className="text-sm leading-relaxed">Approval may immediately delete this agent when the requirement is met. Deletion cannot be undone. Private chats and files are not shown in this review.</p>
      <p className="text-sm text-muted-foreground">{request.viewer.has_viewed ? 'Your detail review has been recorded.' : 'Reload the details to record your review before approving.'}</p>
      {!request.viewer.can_approve ? <p className="text-sm text-muted-foreground">You are not eligible to approve this request.</p> : null}
      <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" className="size-5 accent-primary" checked={confirmed} disabled={!canApprove} onChange={(event) => setConfirmed(event.target.checked)} />I understand that approval can permanently delete {request.agent_name}.</label>
      <Button type="button" variant="destructive" disabled={!canApprove || !confirmed} onClick={() => {
        if (!canApprove || !confirmed || submitting.current) return
        submitting.current = true
        approval.mutate(undefined, { onSettled: () => { submitting.current = false; setConfirmed(false) } })
      }}>{approval.isPending ? 'Approving...' : 'Approve deletion'}</Button>
    </CardContent></Card> : null}
  </>
}

export function AgentDeletionRequestDetailPage() {
  const { id } = useParams()
  const parsed = agentDeletionIdSchema.safeParse(id)
  const detail = useAgentDeletionDetail(parsed.success ? parsed.data : null)
  return <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <header className="flex flex-col items-start gap-3">
      <Button asChild variant="outline"><Link to="/agent-deletion-requests">Back to deletion requests</Link></Button>
      <h1 className="font-display text-2xl font-semibold tracking-tight">Agent deletion request</h1>
    </header>
    {!parsed.success ? <p role="alert" className="text-sm text-destructive">This request link is invalid. Return to deletion requests.</p> : <>
      {detail.isPending ? <Card><CardContent role="status">Loading deletion request...</CardContent></Card> : null}
      {detail.isError ? <Card><CardContent className="flex flex-col items-start gap-3">
        <p role="alert" className="text-sm">Unable to load this request. Check your access and connection, then try again.</p>
        <Button variant="outline" disabled={detail.isFetching} onClick={() => { void detail.refetch() }}>Reload details</Button>
      </CardContent></Card> : null}
      {detail.data ? <DeletionReview key={detail.data.id} request={detail.data} ready={detail.isSuccess && detail.isFetchedAfterMount && !detail.isFetching} /> : null}
    </>}
  </div>
}
