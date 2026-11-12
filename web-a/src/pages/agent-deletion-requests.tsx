import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { useAgentDeletionRequests } from '@/hooks/use-agent-deletions'
import { agentDeletionStatusSchema, type AgentDeletionStatus } from '@/lib/agent-deletion-api'
import { useTranslation } from '@/hooks/use-translation'

export const deletionStatusLabels = { pending: 'Pending', executed: 'Executed', cancelled: 'Cancelled' } as const

export function AgentDeletionRequestsPage() {
  const { t, date, number } = useTranslation()
  const statusId = useId()
  const [status, setStatus] = useState<AgentDeletionStatus>('pending')
  const [cursors, setCursors] = useState<readonly (string | null)[]>([null])
  const cursor = cursors.at(-1) ?? null
  const requests = useAgentDeletionRequests({ status, cursor })
  const nextCursor = requests.data?.next_cursor

  return <div className="admission-content mx-auto flex w-full max-w-3xl flex-col gap-6 [overflow-wrap:anywhere]">
    <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Agent deletion requests')}</h1>
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="flex flex-col gap-2 text-sm font-medium"><label htmlFor={statusId}>{t('Status')}</label>
        <Select id={statusId} value={status} onChange={(event) => { setStatus(agentDeletionStatusSchema.parse(event.target.value)); setCursors([null]) }}>
          <option value="pending">{t('Pending')}</option><option value="executed">{t('Executed')}</option><option value="cancelled">{t('Cancelled')}</option>
        </Select>
      </div>
      <Button variant="outline" disabled={requests.isFetching} onClick={() => { void requests.refetch() }}>{t('Refresh requests')}</Button>
    </div>
    <p className="text-sm text-muted-foreground">{t('Opening a request records your review. A list view does not count as a review or approval.')}</p>
    {requests.isPending ? <Card><CardContent role="status">{t('Loading deletion requests...')}</CardContent></Card> : null}
    {requests.isError ? <Card><CardContent className="flex flex-col items-start gap-3">
      <ErrorToast error={requests.error} message={t('Unable to load deletion requests. Check your connection and try again.')} />
      <Button variant="outline" disabled={requests.isFetching} onClick={() => { void requests.refetch() }}>{t('Retry')}</Button>
    </CardContent></Card> : null}
    {requests.isSuccess && requests.data.items.length === 0 ? <Card><CardContent className="text-sm text-muted-foreground">
      {t('No {status} requests on this page. Choose another status or return to the previous page.', { status: t(deletionStatusLabels[status]).toLowerCase() })}
    </CardContent></Card> : null}
    {requests.isSuccess ? <ul className="flex flex-col gap-4" aria-label={t('Agent deletion requests')}>
      {requests.data.items.map((request) => <li key={request.id}><Card><CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="font-display text-lg font-semibold">{request.agent_name}</h2><Badge variant="secondary">{t(deletionStatusLabels[request.status])}</Badge>
        </div>
        <p className="text-sm text-muted-foreground">{t('Requested by {name}', { name: request.requester.display_name ?? request.requester.id })}</p>
        <p className="text-sm">{t('{count} / {required} counted org-admin approvals · Genesis {status}', { count: number(request.progress.counted_approvals), required: number(request.progress.required_approvals), status: t(request.progress.genesis_approved ? 'approved' : 'not approved') })}</p>
        <time className="text-xs text-muted-foreground" dateTime={request.created_at}>{date(request.created_at)}</time>
        <Button asChild className="self-start"><Link to={`/agent-deletion-requests/${request.id}`} aria-label={t('Review deletion of {name}', { name: request.agent_name })}>{t('Review request')}</Link></Button>
      </CardContent></Card></li>)}
    </ul> : null}
    <nav aria-label={t('Deletion request pages')} className="flex flex-wrap items-center justify-between gap-3">
      <Button variant="outline" disabled={cursors.length === 1 || requests.isFetching} onClick={() => setCursors((values) => values.slice(0, -1))}>{t('Previous')}</Button>
      <p role="status" className="text-sm text-muted-foreground">{t('Page {page}', { page: number(cursors.length) })}</p>
      <Button variant="outline" disabled={!nextCursor || requests.isFetching || requests.isError} onClick={() => { if (nextCursor) setCursors((values) => [...values, nextCursor]) }}>{t('Next')}</Button>
    </nav>
  </div>
}
