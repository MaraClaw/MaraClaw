import { ArrowUpCircle } from 'lucide-react'
import { Link } from 'react-router-dom'

import { OpenClawUpdateCard } from '@/components/openclaw-update-card'
import { Button } from '@/components/ui/button'
import { upgradeOpenClawOptions } from '@/hooks/openclaw-update-options'
import { useMutation, useWorkspaceSession } from '@/hooks/session-workspace'
import { useOpenClawUpdate } from '@/hooks/use-openclaw-update'
import { userFacingRequestError } from '@/lib/http'
import { updateNoticePath } from '@/lib/openclaw-update-policy'

export function AgentOpenClawUpgrade({ agentId, status, query }: {
  readonly agentId: string
  readonly status: string
  readonly query: ReturnType<typeof useOpenClawUpdate>
}) {
  const scope = useWorkspaceSession()
  const upgrade = useMutation(upgradeOpenClawOptions(scope, agentId))
  const update = query.data
  const active = query.gate === 'upgrading'
  const completed = update?.run?.state === 'succeeded' && !update.required

  return (
    <section aria-label="OpenClaw upgrade" className="space-y-3">
      {query.isError ? <div className="space-y-3 rounded-xl border border-border p-4">
        <p role="alert" className="text-sm text-destructive">Unable to check the OpenClaw upgrade status. Work stays paused until status is confirmed.</p>
        <Button variant="outline" disabled={query.isFetching} onClick={() => void query.refetch()}>Retry status check</Button>
      </div> : null}
      {!update && !query.isError ? <p role="status" className="text-sm text-muted-foreground">Checking OpenClaw upgrade status…</p> : null}
      {update ? <OpenClawUpdateCard update={update} submitting={query.submitting}>
        {upgrade.isError ? <p role="alert" className="text-sm text-destructive">{userFacingRequestError(upgrade.error, 'Unable to start the upgrade. Check status and try Upgrade again.')}</p> : null}
        {update.notice ? <>
          {!completed && !active && !update.can_upgrade ? <p className="text-sm text-muted-foreground">Contact an agent manager if you cannot upgrade this agent.</p> : null}
          {!completed && !active && status !== 'running' ? <p className="text-sm text-muted-foreground">{update.can_upgrade ? 'Start this agent using Start below, then choose Upgrade.' : 'An agent manager must start this agent before upgrading.'} Opening this notice does not restart it.</p> : null}
          <div className="flex flex-wrap gap-2">
            <Button
              className="h-auto min-h-11 whitespace-normal"
              disabled={completed || active || query.gate === 'checking' || query.isError || !update.can_upgrade || status !== 'running'}
              onClick={() => { if (update.notice) upgrade.mutate(update.notice.id) }}
            >
              <ArrowUpCircle className="size-4 shrink-0" aria-hidden />
              {active ? 'Upgrading…' : completed ? 'Upgraded' : update.run?.state === 'failed' ? 'Retry Upgrade' : 'Upgrade'}
            </Button>
            <Button asChild variant="outline" className="h-auto min-h-11 whitespace-normal"><Link to={updateNoticePath(update.notice.id)}>Read update notice</Link></Button>
          </div>
        </> : null}
      </OpenClawUpdateCard> : null}
    </section>
  )
}
