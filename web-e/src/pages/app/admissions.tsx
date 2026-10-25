import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { listMyAdmissionsPage } from '@/lib/company-admissions-api'
import { ApiError, formatApiDetail, userFacingRequestError } from '@/lib/http'
import { relativeTime } from '@/lib/relative-time'
import { admissionStatusSchema, type AdmissionStatus } from '@/lib/types/company-admissions'
import { admissionStatuses } from './admission-labels'
import { TransferRequests } from './transfer-requests'

const pageSize = 50

export function AdmissionsPage() {
  const scope = useWorkspaceSession()
  const [status, setStatus] = useState<AdmissionStatus | 'all'>('all')
  const [cursors, setCursors] = useState<readonly string[]>([])
  const [refreshVersion, setRefreshVersion] = useState(0)
  const [notice, setNotice] = useState('')
  const cursor = cursors.at(-1)
  const query = useQuery({
    queryKey: ['company-admissions', 'mine', 'page', status, cursor ?? null, refreshVersion],
    queryFn: ({ signal }) => listMyAdmissionsPage({
      limit: pageSize, ...(status === 'all' ? {} : { status }), ...(cursor === undefined ? {} : { cursor }),
    }, { owner: scope.owner, signal }),
    retry: false,
  })
  const invalidCursor = cursor !== undefined && query.error instanceof ApiError
    && query.error.status === 400 && formatApiDetail(query.error.detail) === 'Invalid cursor'
  useEffect(() => {
    if (!invalidCursor) return
    scope.effect(() => {
      setCursors([])
      setRefreshVersion((version) => version + 1)
      setNotice('History changed. Showing the latest requests.')
    })()
  }, [invalidCursor, scope])
  // A new refresh key forces a root request even while the old root is fresh in cache.
  // All versions stay under the mine prefix for withdrawal invalidation.
  const refresh = () => {
    setCursors([])
    setRefreshVersion((version) => version + 1)
    setNotice('')
  }
  const page = query.isSuccess && !query.isFetching ? query.data : undefined

  return (
    <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-semibold">Organization admissions</h1>
        <Button variant="outline" className="h-auto min-h-11 whitespace-normal" disabled={query.isFetching} onClick={scope.effect(refresh)}>Refresh status</Button>
      </div>
      <p className="text-sm text-muted-foreground">Your organization admission history. New invitations join automatically; paid transfers join after payment approval. Historical pending and rejected requests remain here.</p>
      <TransferRequests refreshVersion={refreshVersion} />
      <h2 className="font-display text-lg font-semibold">Destination admission requests</h2>
      <div className="max-w-xs space-y-2">
        <Label htmlFor="admission-status">Request status</Label>
        <Select id="admission-status" value={status} onChange={(event) => {
          const value = event.currentTarget.value
          setStatus(value === 'all' ? 'all' : admissionStatusSchema.parse(value))
          setCursors([])
          setNotice('')
        }}>
          <option value="all">All statuses</option>
          {admissionStatusSchema.options.map((value) => <option key={value} value={value}>{admissionStatuses[value].label}</option>)}
        </Select>
      </div>
      {notice ? <p role="status" className="text-sm text-muted-foreground">{notice}</p> : null}
      {query.isFetching || query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading requests…</p> : null}
      {query.isError && !invalidCursor ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load requests. Refresh to try again.')}</p> : null}
      {page?.items.length === 0 ? (
        <div className="space-y-3 rounded-xl border border-border p-4">
          <p className="text-sm text-muted-foreground">{cursor !== undefined ? 'No more requests on this page. Go back or refresh to see the latest requests.' : status === 'all' ? 'No admission requests yet.' : `No ${admissionStatuses[status].label.toLowerCase()} requests match this filter.`}</p>
          {status === 'all' ? <Button asChild variant="outline"><Link to="/transfer">Request a company transfer</Link></Button> : <Button variant="outline" onClick={() => { setStatus('all'); setCursors([]) }}>Show all statuses</Button>}
        </div>
      ) : null}
      <ul aria-label="Admission requests" aria-busy={query.isFetching} className="space-y-3">
        {page?.items.map((request) => (
          <li key={request.id} className="space-y-3 rounded-xl border border-border p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-medium">{request.target_tenant.name}</h2>
              <Badge variant="outline">{admissionStatuses[request.status].label}</Badge>
            </div>
            <p className="text-xs text-muted-foreground">Submitted <time dateTime={request.created_at} title={new Date(request.created_at).toLocaleString()}>{relativeTime(request.created_at)}</time></p>
            <Link className="inline-flex min-h-11 items-center rounded-lg py-2 text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-ring" to={`/app/admissions/${encodeURIComponent(request.id)}`}>
              View request for {request.target_tenant.name}
            </Link>
          </li>
        ))}
      </ul>
      <nav aria-label="Admission request pages" className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" disabled={cursors.length === 0 || query.isFetching} onClick={() => setCursors((stack) => stack.slice(0, -1))}>Back</Button>
        <span aria-live="polite" className="text-sm text-muted-foreground">Page {cursors.length + 1}</span>
        <Button variant="outline" disabled={!page?.next_cursor} onClick={() => {
          const nextCursor = page?.next_cursor
          if (nextCursor) setCursors((stack) => [...stack, nextCursor])
        }}>Older requests</Button>
      </nav>
      <p className="text-xs text-muted-foreground">Newest requests first. Statuses can change while you browse; refresh to see the latest requests.</p>
    </div>
  )
}
