import { useInfiniteQuery } from '@/hooks/session-workspace'
import { Loader2, Plus } from 'lucide-react'
import { Link } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
import { AgentAccessExplanation } from '@/components/agent-access-explanation'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { agentPageOptions, distinctAgents } from '@/lib/agents-query'

export function AgentsListPage() {
  const query = useInfiniteQuery(agentPageOptions())
  const agents = distinctAgents(query.data?.pages)

  return (
    <div className="space-y-5 p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold">Agents</h1>
          <p className="text-sm text-muted-foreground">Digital employees you can chat with in this company.</p>
        </div>
        <Button asChild>
          <Link to="/app/agents/new">
            <Plus className="size-4" />
            New agent
          </Link>
        </Button>
      </div>

      {query.isLoading ? (
        <div className="flex h-32 items-center justify-center text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
        </div>
      ) : null}

      {query.isError && !query.isFetchNextPageError ? (
        <div className="space-y-3">
          <RequestErrorToast error={query.error} message="Could not load agents. Try again." />
          <Button variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}>Retry loading agents</Button>
        </div>
      ) : null}

      {query.isSuccess && agents.length === 0 ? (
        <Card>
          <CardContent className="py-10 text-sm text-muted-foreground">
            No agents yet.{' '}
            <Link to="/app/onboarding" className="font-medium text-primary underline-offset-4 hover:underline">
              Create a personal assistant
            </Link>{' '}
            or start from a template.
          </CardContent>
        </Card>
      ) : null}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {agents.map((agent) => (
          <Link key={agent.id} to={`/app/agents/${agent.id}/chat`}>
            <Card className="h-full transition-colors hover:border-primary/40">
              <CardHeader>
                <CardTitle className="flex items-center justify-between gap-2 text-base">
                  <span className="truncate">{agent.name}</span>
                  <Badge variant="soft">{agent.status}</Badge>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm text-muted-foreground">
                <p className="line-clamp-2">{agent.role_description || 'No role description'}</p>
                <AgentAccessExplanation reason={agent.access_reason} />
                {agent.unread_count ? <p>{agent.unread_count} unread</p> : null}
                {agent.is_expired ? <p className="text-destructive">Expired</p> : null}
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
      {query.hasNextPage ? (
        <div className="space-y-3">
          {query.isFetchNextPageError ? <RequestErrorToast error={query.error} message="Could not load more agents. Your loaded agents are still available." /> : null}
          <Button variant="outline" disabled={query.isFetching} onClick={() => void query.fetchNextPage()}>
            {query.isFetchingNextPage ? 'Loading more agents...' : query.isFetchNextPageError ? 'Retry loading more' : 'Load more'}
          </Button>
        </div>
      ) : null}
    </div>
  )
}
