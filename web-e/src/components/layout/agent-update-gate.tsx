import { Link } from 'react-router-dom'

import { OpenClawUpdateCard } from '@/components/openclaw-update-card'
import { Button } from '@/components/ui/button'
import { useOpenClawUpdate } from '@/hooks/use-openclaw-update'
import { agentSettingsPath } from '@/lib/openclaw-update-policy'

export function AgentUpdateGate({ agentId, query }: {
  readonly agentId: string
  readonly query: ReturnType<typeof useOpenClawUpdate>
}) {
  const settingsLink = <Button asChild className="h-auto min-h-11 whitespace-normal"><Link to={agentSettingsPath(agentId)}>Open agent Settings</Link></Button>
  return (
    <div className="mx-auto max-w-xl space-y-4 p-6">
      <h1 className="font-display text-xl font-semibold">Agent work is paused</h1>
      {query.isError ? <div className="space-y-3">
        <p role="alert" className="text-sm text-destructive">Unable to confirm OpenClaw upgrade status. Check your connection and retry before continuing work.</p>
        <Button variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}>Retry status check</Button>
      </div> : null}
      {query.data ? <OpenClawUpdateCard update={query.data} submitting={query.submitting}>
        {query.data.can_upgrade ? <p className="text-sm">Choose Upgrade in Settings, then wait for completion.</p> : <p className="text-sm">Contact an agent manager to complete the upgrade.</p>}
        {settingsLink}
      </OpenClawUpdateCard> : <>
        {!query.isError ? <p role="status" className="text-sm text-muted-foreground">Checking OpenClaw upgrade status…</p> : null}
        {settingsLink}
      </>}
    </div>
  )
}
