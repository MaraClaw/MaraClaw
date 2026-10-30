import { useEffect, useState } from 'react'
import { Link, useOutletContext } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useInfiniteQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { listAgentActivity } from '@/lib/activity-api'
import { ApiError } from '@/lib/http'
import { relativeTime } from '@/lib/relative-time'
import type { AgentOut } from '@/lib/workspace-api'

export function AgentActivityPage() {
  const { agent } = useOutletContext<{ agent: AgentOut }>()
  if (agent.access_level !== 'manage') {
    return (
      <section className="space-y-4 p-6" aria-labelledby="activity-heading">
        <h1 id="activity-heading" className="font-display text-xl font-semibold">Activity</h1>
        <p className="text-sm text-muted-foreground">Only agent managers can view activity. Ask a manager for access.</p>
        <Button variant="outline" asChild><Link to={`/app/agents/${encodeURIComponent(agent.id)}/chat`}>Return to chat</Link></Button>
      </section>
    )
  }
  return <ManagerActivity key={agent.id} agent={agent} />
}

function ManagerActivity({ agent }: { readonly agent: AgentOut }) {
  const scope = useWorkspaceSession()
  const [draft, setDraft] = useState('')
  const [filter, setFilter] = useState('')
  const key = ['agent-activity', agent.id] as const
  const history = useInfiniteQuery<Awaited<ReturnType<typeof listAgentActivity>>, Error, string | null>({
    queryKey: [...key, filter],
    initialPageParam: null,
    queryFn: ({ pageParam, signal }) => listAgentActivity(agent.id, {
      cursor: pageParam, owner: scope.owner, signal,
      ...(filter === '' ? {} : { actionType: filter }),
    }),
    getNextPageParam: (page, _pages, _param, params) =>
      page.next_cursor !== null && !params.includes(page.next_cursor) ? page.next_cursor : undefined,
    maxPages: 5,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
  })
  const denied = history.error instanceof ApiError && history.error.status === 403

  useEffect(() => {
    if (!denied || !scope.isCurrent()) return
    // Summaries are sensitive: evict every filter, then retire the stale manage grant.
    scope.client.removeQueries({ queryKey: ['agent-activity', agent.id] })
    scope.client.setQueryData<AgentOut>(['agent', agent.id], (current) =>
      current ? { ...current, access_level: 'use' } : current)
  }, [denied, scope, agent.id])

  if (denied) return <p role="alert" className="p-6 text-sm text-muted-foreground">Activity access is no longer available. Ask a manager for access.</p>

  const items = [...new Map(history.data?.pages.flatMap((page) => page.items).map((item) => [item.id, item])).values()]
  const olderError = history.isFetchNextPageError
  const windowed = history.data?.pageParams[0] != null

  return (
    <section className="max-w-2xl space-y-6 p-6" aria-labelledby="activity-heading">
      <h1 id="activity-heading" className="font-display text-xl font-semibold">Activity</h1>
      <form className="space-y-2" onSubmit={scope.effect((event) => {
        event.preventDefault()
        setFilter(draft)
      })}>
        <Label htmlFor="activity-type">Action type (exact match)</Label>
        <div className="flex flex-wrap gap-2">
          <Input id="activity-type" name="action_type" className="w-full sm:w-auto sm:flex-1" value={draft}
            onChange={scope.effect((event) => setDraft(event.target.value))} aria-describedby="activity-filter-help" />
          <Button type="submit">Apply filter</Button>
          <Button type="button" variant="outline" onClick={scope.effect(() => { setDraft(''); setFilter('') })}>Clear filter</Button>
        </div>
        <p id="activity-filter-help" className="text-sm text-muted-foreground">Leave blank to show all action types.</p>
      </form>
      <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
        {history.isPaused ? 'You’re offline. Activity will load when you reconnect.'
          : history.isPending ? 'Loading activity…'
          : history.isFetchingNextPage ? 'Loading older activity…'
          : history.isError ? ''
          : items.length === 0 ? 'No activity found. Clear the filter or refresh to check again.'
          : windowed ? 'Showing up to 100 loaded actions. Refresh to return to the latest.'
          : 'Newest activity first.'}
      </p>
      {items.length > 0 ? (
        <ol aria-label="Agent activity" className="divide-y divide-border">
          {items.map((item) => {
            const timestamp = item.created_at === null ? Number.NaN : Date.parse(item.created_at)
            const createdAt = Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null
            return (
              <li key={item.id} className="space-y-2 py-4 first:pt-0">
                <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm text-muted-foreground">
                  <span className="min-w-0 [overflow-wrap:anywhere]">{item.action_type || 'Unknown action'}</span>
                  {createdAt === null ? <span>Time unavailable</span> : (
                    <time dateTime={createdAt} title={new Date(createdAt).toLocaleString()}>{relativeTime(createdAt)}</time>
                  )}
                </div>
                <p className="whitespace-pre-wrap text-base leading-relaxed [overflow-wrap:anywhere] sm:text-sm">{item.summary}</p>
              </li>
            )
          })}
        </ol>
      ) : null}
      {history.isError ? <p role="alert" className="text-sm text-destructive">
        {olderError ? 'Could not load older activity. Your loaded actions are still available.' : 'Could not load activity. Please try again.'}
      </p> : null}
      <div className="flex flex-wrap gap-2">
        {history.hasNextPage ? (
          <Button type="button" variant="outline" className="min-w-40" disabled={history.isFetching || history.isPaused}
            onClick={() => { if (!history.isFetching) void history.fetchNextPage() }}>
            {olderError ? 'Retry older activity' : 'Load older activity'}
          </Button>
        ) : null}
        <Button type="button" variant="outline" disabled={history.isFetching || history.isPaused}
          onClick={scope.effect(() => { void scope.client.resetQueries({ queryKey: [...key, filter], exact: true }) })}>
          {history.isError && !olderError ? 'Retry activity' : 'Refresh activity'}
        </Button>
      </div>
    </section>
  )
}
