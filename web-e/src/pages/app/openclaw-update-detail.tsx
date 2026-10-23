import { ArrowLeft } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'

import { OpenClawUpdateCard } from '@/components/openclaw-update-card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { agentSettingsPath, isActiveUpdate } from '@/lib/openclaw-update-policy'
import { getOpenClawUpdate } from '@/lib/openclaw-updates-api'

export function OpenClawUpdateDetailPage() {
  const { noticeId = '' } = useParams()
  const scope = useWorkspaceSession()
  const query = useQuery({
    queryKey: ['openclaw-update-detail', noticeId],
    queryFn: ({ signal }) => getOpenClawUpdate(noticeId, { owner: scope.owner, signal }),
    enabled: Boolean(noticeId),
    refetchInterval: (result) => result.state.data?.agents.some((agent) => isActiveUpdate(agent.update.run)) ? 1500 : 30_000,
    refetchOnWindowFocus: true,
  })

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6 [overflow-wrap:anywhere]">
      <Button asChild variant="outline"><Link to="/app/notifications"><ArrowLeft aria-hidden="true" className="size-4" />Back to Inbox</Link></Button>
      {query.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading update notice…</p> : null}
      {query.isError ? <div className="space-y-3">
        <p role="alert" className="text-sm text-destructive">{userFacingRequestError(query.error, 'Unable to load this update notice. Check your connection and try again.')}</p>
        <Button variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}>Retry</Button>
      </div> : null}
      {query.data ? <Card className="gap-6 px-6">
        <header className="space-y-3">
          <Badge variant="soft">{query.data.notice.mandatory ? 'Mandatory' : 'Optional'}</Badge>
          <h1 className="font-display text-2xl font-semibold">{query.data.notice.title}</h1>
          <time className="block text-xs text-muted-foreground" dateTime={query.data.notice.created_at}>{new Date(query.data.notice.created_at).toLocaleString()}</time>
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{query.data.notice.body}</p>
        </header>
        <section aria-label="Agents for this update" className="space-y-4">
          <h2 className="font-display text-lg font-semibold">Your agents</h2>
          <p className="text-sm text-muted-foreground">Open an agent’s Settings and choose Upgrade. Once started, wait for completion before continuing work.</p>
          {query.data.agents.length === 0 ? <p className="text-sm text-muted-foreground">No agents you can access are associated with this update.</p> : null}
          {query.data.agents.map((agent) => <OpenClawUpdateCard key={agent.agent_id} heading={agent.name} update={agent.update}>
            <p className="text-sm text-muted-foreground">Agent status: {agent.status}</p>
            {!agent.update.can_upgrade && agent.update.run?.state !== 'succeeded' ? <p className="text-sm text-muted-foreground">If you only have use access, contact an agent manager to upgrade.</p> : null}
            <Button asChild className="h-auto min-h-11 whitespace-normal"><Link to={agentSettingsPath(agent.agent_id)}>{isActiveUpdate(agent.update.run) ? 'View upgrade in Settings' : agent.update.run?.state === 'succeeded' && !agent.update.required ? 'Open Settings' : 'Upgrade in Settings'}</Link></Button>
          </OpenClawUpdateCard>)}
        </section>
      </Card> : null}
    </div>
  )
}
