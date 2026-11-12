import { useTranslation } from '@/hooks/use-translation'
import { ArrowLeft } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
import { OpenClawUpdateCard } from '@/components/openclaw-update-card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { agentSettingsPath, isActiveUpdate } from '@/lib/openclaw-update-policy'
import { getOpenClawUpdate } from '@/lib/openclaw-updates-api'

export function OpenClawUpdateDetailPage() {
  const { t: translate, owned, formatDate } = useTranslation()
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
      <Button asChild variant="outline"><Link to="/app/notifications"><ArrowLeft aria-hidden="true" className="size-4" />{translate("Back to Inbox")}</Link></Button>
      {query.isPending ? <p role="status" className="text-sm text-muted-foreground">{translate("Loading update notice…")}</p> : null}
      {query.isError ? <div className="space-y-3">
        <RequestErrorToast error={query.error} message={userFacingRequestError(query.error, translate("Unable to load this update notice. Check your connection and try again."))} />
        <Button variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}>{translate("Retry")}</Button>
      </div> : null}
      {query.data ? <Card className="gap-6 px-6">
        <header className="space-y-3">
          <Badge variant="soft">{query.data.notice.mandatory ? translate("Mandatory") : translate("Optional")}</Badge>
          <h1 className="font-display text-2xl font-semibold">{query.data.notice.title}</h1>
          <time className="block text-xs text-muted-foreground" dateTime={query.data.notice.created_at}>{formatDate(query.data.notice.created_at, { dateStyle: 'medium', timeStyle: 'short' })}</time>
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{query.data.notice.body}</p>
        </header>
        <section aria-label={translate("Agents for this update")} className="space-y-4">
          <h2 className="font-display text-lg font-semibold">{translate("Your agents")}</h2>
          <p className="text-sm text-muted-foreground">{translate("Open an agent’s Settings and choose Upgrade. Once started, wait for completion before continuing work.")}</p>
          {query.data.agents.length === 0 ? <p className="text-sm text-muted-foreground">{translate("No agents you can access are associated with this update.")}</p> : null}
          {query.data.agents.map((agent) => <OpenClawUpdateCard key={agent.agent_id} heading={agent.name} update={agent.update}>
          <p className="text-sm text-muted-foreground">{translate("Agent status:")}{' '}{owned(agent.status)}</p>
            {!agent.update.can_upgrade && agent.update.run?.state !== 'succeeded' ? <p className="text-sm text-muted-foreground">{translate("If you only have use access, contact an agent manager to upgrade.")}</p> : null}
            <Button asChild className="h-auto min-h-11 whitespace-normal"><Link to={agentSettingsPath(agent.agent_id)}>{isActiveUpdate(agent.update.run) ? translate("View upgrade in Settings") : agent.update.run?.state === 'succeeded' && !agent.update.required ? translate("Open Settings") : translate("Upgrade in Settings")}</Link></Button>
          </OpenClawUpdateCard>)}
        </section>
      </Card> : null}
    </div>
  )
}
