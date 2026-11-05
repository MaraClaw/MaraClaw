import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useInfiniteQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { listMyTransfersPage } from '@/lib/company-transfers-api'
import { formatMoney } from '@/lib/format-money'
import { userFacingRequestError } from '@/lib/http'
import { transferStatuses } from './transfer-labels'

export function TransferRequests({ refreshVersion }: { readonly refreshVersion: number }) {
  const scope = useWorkspaceSession()
  const query = useInfiniteQuery<Awaited<ReturnType<typeof listMyTransfersPage>>, Error, string | null>({
    queryKey: ['company-transfers', 'mine', 'page', refreshVersion],
    initialPageParam: null,
    queryFn: ({ pageParam, signal }) => listMyTransfersPage({
      limit: 50, ...(pageParam === null ? {} : { cursor: pageParam }),
    }, { owner: scope.owner, signal }),
    getNextPageParam: (page) => page.next_cursor,
    retry: false, refetchOnMount: 'always', gcTime: 0,
  })
  const items = query.data?.pages.flatMap((page) => page.items)
  return <section aria-labelledby="transfer-requests-heading" className="space-y-3">
    <h2 id="transfer-requests-heading" className="font-display text-lg font-semibold">Transfer requests</h2>
    <p className="text-sm text-muted-foreground">Paid transfers need payment review before destination admission. These requests are separate from the admission status filter below.</p>
    {query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading transfer requests…</p> : null}
    {query.isFetchingNextPage ? <p role="status" className="text-sm text-muted-foreground">Loading older transfer requests…</p> : null}
    {query.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load transfer requests. Refresh to try again.')}</p> : null}
    {items?.length === 0 ? <p className="text-sm text-muted-foreground">No transfer requests yet.</p> : null}
    <ul aria-label="Transfer requests" aria-busy={query.isFetching} className="space-y-3">
      {items?.map((request) => <li key={request.id} className="space-y-2 rounded-xl border border-border p-4 text-sm">
        <Badge variant="outline">{transferStatuses[request.status].label}</Badge>
        <p>{formatMoney(request.fee_amount_minor, request.currency)} ({request.currency}) · Submitted {new Date(request.created_at).toLocaleString()}</p>
        <Link className="inline-flex min-h-11 items-center rounded-lg font-medium text-primary hover:underline focus-visible:outline-2 focus-visible:outline-ring" to={`/app/admissions/transfers/${encodeURIComponent(request.id)}`}>View transfer request <span className="sr-only">{request.id}</span></Link>
      </li>)}
    </ul>
    {query.hasNextPage ? <Button type="button" variant="outline" className="min-h-11" disabled={query.isFetching || query.isPaused}
      onClick={() => { if (!query.isFetching) void query.fetchNextPage({ cancelRefetch: false }) }}>Load older</Button> : null}
  </section>
}
