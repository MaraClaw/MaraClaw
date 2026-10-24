import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { listMyTransfers } from '@/lib/company-transfers-api'
import { formatMoney } from '@/lib/format-money'
import { userFacingRequestError } from '@/lib/http'
import { transferStatuses } from './transfer-labels'

export function TransferRequests({ refreshVersion }: { readonly refreshVersion: number }) {
  const scope = useWorkspaceSession()
  const query = useQuery({
    queryKey: ['company-transfers', 'mine', refreshVersion],
    queryFn: ({ signal }) => listMyTransfers({ owner: scope.owner, signal }),
    retry: false, refetchOnMount: 'always',
  })
  return <section aria-labelledby="transfer-requests-heading" className="space-y-3">
    <h2 id="transfer-requests-heading" className="font-display text-lg font-semibold">Transfer requests</h2>
    <p className="text-sm text-muted-foreground">Paid transfers need payment review before destination admission. These requests are separate from the admission status filter below.</p>
    {query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading transfer requests…</p> : null}
    {query.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load transfer requests. Refresh to try again.')}</p> : null}
    {query.data?.length === 0 ? <p className="text-sm text-muted-foreground">No transfer requests yet.</p> : null}
    <ul aria-label="Transfer requests" aria-busy={query.isFetching} className="space-y-3">
      {query.data?.map((request) => <li key={request.id} className="space-y-2 rounded-xl border border-border p-4 text-sm">
        <Badge variant="outline">{transferStatuses[request.status].label}</Badge>
        <p>{formatMoney(request.fee_amount_minor, request.currency)} ({request.currency}) · Submitted {new Date(request.created_at).toLocaleString()}</p>
        <Link className="inline-flex min-h-11 items-center rounded-lg font-medium text-primary hover:underline focus-visible:outline-2 focus-visible:outline-ring" to={`/app/admissions/transfers/${encodeURIComponent(request.id)}`}>View transfer request <span className="sr-only">{request.id}</span></Link>
      </li>)}
    </ul>
    {query.data?.length === 100 ? <p className="text-xs text-muted-foreground">Showing the latest 100 transfer requests. Older requests remain available through their saved links.</p> : null}
  </section>
}
