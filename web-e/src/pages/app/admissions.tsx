import { useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { listMyAdmissions } from '@/lib/company-admissions-api'
import { userFacingRequestError } from '@/lib/http'
import { relativeTime } from '@/lib/relative-time'
import { admissionStatuses } from './admission-labels'

const pageSize = 50

export function AdmissionsPage() {
  const scope = useWorkspaceSession()
  const [offset, setOffset] = useState(0)
  const query = useQuery({
    queryKey: ['company-admissions', 'mine', offset],
    queryFn: ({ signal }) => listMyAdmissions({ limit: pageSize, offset }, { owner: scope.owner, signal }),
  })

  return (
    <div className="admission-content mx-auto max-w-2xl space-y-4 p-6 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-semibold">Organization admissions</h1>
        <Button variant="outline" className="h-auto min-h-11 whitespace-normal" disabled={query.isFetching} onClick={() => void query.refetch()}>Refresh status</Button>
      </div>
      <p className="text-sm text-muted-foreground">Track your company admission requests. Pending and rejected requests do not change your current organization.</p>
      {query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading requests…</p> : null}
      {query.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load requests. Refresh to try again.')}</p> : null}
      {query.data?.length === 0 ? (
        <div className="space-y-3 rounded-xl border border-border p-4">
          <p className="text-sm text-muted-foreground">No admission requests on this page.</p>
          <Button asChild variant="outline"><Link to="/transfer">Request a company transfer</Link></Button>
        </div>
      ) : null}
      <ul className="space-y-3">
        {query.data?.map((request) => (
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
      <nav aria-label="Admission request pages" className="flex items-center justify-between gap-3">
        <Button variant="outline" disabled={offset === 0 || query.isFetching} onClick={() => setOffset(offset - pageSize)}>Previous</Button>
        <span className="text-sm text-muted-foreground">Page {offset / pageSize + 1}</span>
        <Button variant="outline" disabled={query.isFetching || query.isError || (query.data?.length ?? 0) < pageSize} onClick={() => setOffset(offset + pageSize)}>Next</Button>
      </nav>
    </div>
  )
}
