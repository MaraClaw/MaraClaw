import { useQuery } from '@tanstack/react-query'
import { Loader2, Plus } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { listAgents } from '@/lib/workspace-api'

export function AgentsListPage() {
  const query = useQuery({ queryKey: ['agents'], queryFn: listAgents })

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

      {query.data?.length === 0 ? (
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

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {(query.data ?? []).map((agent) => (
          <Link key={agent.id} to={`/app/agents/${agent.id}/chat`}>
            <Card className="h-full transition-colors hover:border-primary/40">
              <CardHeader>
                <CardTitle className="flex items-center justify-between gap-2 text-base">
                  <span className="truncate">{agent.name}</span>
                  <span className="flex shrink-0 items-center gap-1.5">
                    <Badge variant="outline">{agent.agent_type === 'openclaw' ? 'OpenClaw' : 'Native'}</Badge>
                    <Badge variant="soft">{agent.status}</Badge>
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm text-muted-foreground">
                <p className="line-clamp-2">{agent.role_description || 'No role description'}</p>
                {agent.unread_count ? <p>{agent.unread_count} unread</p> : null}
                {agent.is_expired ? <p className="text-destructive">Expired</p> : null}
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  )
}
