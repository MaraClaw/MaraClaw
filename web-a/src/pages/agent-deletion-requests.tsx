import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { useAgentDeletionRequests } from '@/hooks/use-agent-deletions'
import { agentDeletionStatusSchema, type AgentDeletionStatus } from '@/lib/agent-deletion-api'

export function AgentDeletionRequestsPage() {
  const statusId = useId()
  const [status, setStatus] = useState<AgentDeletionStatus>('pending')
  const [cursors, setCursors] = useState<readonly (string | null)[]>([null])
  const cursor = cursors.at(-1) ?? null
  const requests = useAgentDeletionRequests({ status, cursor })
  const nextCursor = requests.data?.next_cursor

  return <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <h1 className="font-display text-2xl font-semibold tracking-tight">Agent deletion requests</h1>
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="flex flex-col gap-2 text-sm font-medium"><label htmlFor={statusId}>Status</label>
        <Select id={statusId} value={status} onChange={(event) => { setStatus(agentDeletionStatusSchema.parse(event.target.value)); setCursors([null]) }}>
          <option value="pending">Pending</option><option value="executed">Executed</option><option value="cancelled">Cancelled</option>
        </Select>
      </div>
      <Button variant="outline" disabled={requests.isFetching} onClick={() => { void requests.refetch() }}>Refresh requests</Button>
    </div>
    <p className="text-sm text-muted-foreground">Opening a request records your review. A list view does not count as a review or approval.</p>
    {requests.isPending ? <Card><CardContent role="status">Loading deletion requests...</CardContent></Card> : null}
    {requests.isError ? <Card><CardContent className="flex flex-col items-start gap-3">
      <p role="alert" className="text-sm">Unable to load deletion requests. Check your connection and try again.</p>
      <Button variant="outline" disabled={requests.isFetching} onClick={() => { void requests.refetch() }}>Retry</Button>
    </CardContent></Card> : null}
    {requests.isSuccess && requests.data.items.length === 0 ? <Card><CardContent className="text-sm text-muted-foreground">
      No {status} requests on this page. Choose another status or return to the previous page.
    </CardContent></Card> : null}
    {requests.isSuccess ? <ul className="flex flex-col gap-4" aria-label="Agent deletion requests">
      {requests.data.items.map((request) => <li key={request.id}><Card><CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="font-display text-lg font-semibold">{request.agent_name}</h2><Badge variant="secondary">{request.status}</Badge>
        </div>
        <p className="text-sm text-muted-foreground">Requested by {request.requester.display_name ?? request.requester.id}</p>
        <p className="text-sm">{request.progress.counted_approvals} / {request.progress.required_approvals} counted org-admin approvals · Genesis {request.progress.genesis_approved ? 'approved' : 'not approved'}</p>
        <time className="text-xs text-muted-foreground" dateTime={request.created_at}>{new Date(request.created_at).toLocaleString()}</time>
        <Button asChild className="self-start"><Link to={`/agent-deletion-requests/${request.id}`} aria-label={`Review deletion of ${request.agent_name}`}>Review request</Link></Button>
      </CardContent></Card></li>)}
    </ul> : null}
    <nav aria-label="Deletion request pages" className="flex flex-wrap items-center justify-between gap-3">
      <Button variant="outline" disabled={cursors.length === 1 || requests.isFetching} onClick={() => setCursors((values) => values.slice(0, -1))}>Previous</Button>
      <p role="status" className="text-sm text-muted-foreground">Page {cursors.length}</p>
      <Button variant="outline" disabled={!nextCursor || requests.isFetching || requests.isError} onClick={() => { if (nextCursor) setCursors((values) => [...values, nextCursor]) }}>Next</Button>
    </nav>
  </div>
}
