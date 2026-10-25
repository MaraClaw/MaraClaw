import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { invitationHistoryOptions } from '@/hooks/invitation-options'
import { InvitationsRow } from './invitations-row'

export function InvitationsHistory({ tenant }: { readonly tenant: string | null }) {
  const [page, setPage] = useState(1)
  const history = useQuery(invitationHistoryOptions(tenant, page, true))
  const data = history.data
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : page
  return (
    <Card>
      <CardHeader className="gap-2">
        <h2 className="font-display text-lg font-semibold">Invitation history</h2>
        <p role="status" className="text-sm text-muted-foreground">{history.isFetching ? data ? 'Refreshing history…' : 'Loading invitations…' : data ? `${data.total} invitation${data.total === 1 ? '' : 's'}` : null}</p>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">
        {history.isError ? <div role="alert" className="flex flex-wrap items-center gap-3 text-sm"><p className="text-destructive">Could not load invitation history.{data ? ' Previously loaded rows are kept below.' : ''}</p><Button type="button" variant="outline" className="min-h-11" disabled={history.isFetching} onClick={() => { void history.refetch() }}>Retry history</Button></div> : null}
        {data?.items.length ? (
          <table aria-label="Invitation history" className="block w-full table-fixed text-sm [overflow-wrap:anywhere] lg:table">
            <thead className="sr-only text-left text-xs text-muted-foreground lg:not-sr-only lg:table-header-group"><tr>{['Recipient / code', 'Created', 'Expires', 'Use', 'Status', 'Action'].map((heading) => <th key={heading} scope="col" className="px-4 pb-3 font-medium first:lg:w-1/4 last:lg:w-1/5">{heading}</th>)}</tr></thead>
            <tbody className="block lg:table-row-group">{data.items.map((item) => <InvitationsRow key={item.id} item={item} tenant={tenant} />)}</tbody>
          </table>
        ) : data ? <p className="py-5 text-sm text-muted-foreground">{data.total ? 'No invitations on this page. Use Previous to return to earlier results.' : 'No invitations yet. Send an invitation email above to invite someone to your organization.'}</p> : null}
        {(data && data.total > data.page_size) || page > 1 ? (
          <nav aria-label="Invitation history pages" className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
            <p className="text-sm text-muted-foreground">{data ? `Page ${page} of ${pages}` : `Page ${page}`}</p>
            <div className="flex gap-2"><Button type="button" variant="outline" className="min-h-11" disabled={page <= 1 || history.isFetching} onClick={() => setPage(page - 1)}>Previous</Button><Button type="button" variant="outline" className="min-h-11" disabled={!data || page >= pages || history.isFetching} onClick={() => setPage(page + 1)}>Next</Button></div>
          </nav>
        ) : null}
      </CardContent>
    </Card>
  )
}
