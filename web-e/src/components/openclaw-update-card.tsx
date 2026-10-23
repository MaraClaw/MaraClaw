import type { ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { assertNever } from '@/lib/openclaw-update-policy'
import type { AgentUpdate, OpenClawRun } from '@/lib/openclaw-updates-api'

function runStatus(run: OpenClawRun): string {
  switch (run.state) {
    case 'pending': return 'Upgrade queued. Wait for it to finish before continuing work.'
    case 'cutover': return 'Upgrade in progress. Wait for it to finish before continuing work.'
    case 'succeeded': return 'Upgrade completed.'
    case 'failed': return 'Upgrade failed. Choose Retry Upgrade in agent Settings. If it fails again, contact your agent manager.'
    default: return assertNever(run.state)
  }
}

export function OpenClawUpdateCard({ update, heading = 'OpenClaw upgrade', submitting = false, children }: {
  readonly update: AgentUpdate
  readonly heading?: string
  readonly submitting?: boolean
  readonly children: ReactNode
}) {
  return (
    <Card className="gap-4 bg-accent/40 [overflow-wrap:anywhere]">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-display text-base font-semibold">{heading}</h2>
          {update.notice ? <Badge variant="soft">{update.notice.mandatory ? 'Mandatory' : 'Optional'}</Badge> : null}
        </div>
        {update.notice ? <p className="text-sm font-medium">{update.notice.title}</p> : null}
      </CardHeader>
      <CardContent className="space-y-4">
        <div role="status" aria-live="polite" aria-atomic="true" className="space-y-2 text-sm leading-relaxed">
          <p>{submitting ? 'Requesting upgrade. Please wait…' : update.run ? runStatus(update.run) : update.notice ? 'An OpenClaw upgrade is available.' : 'OpenClaw is up to date.'}</p>
          {update.required ? <p>You must complete this upgrade before continuing work with this agent.</p> : null}
          {update.run?.failure_code ? <p>Failure reference: <code className="font-mono">{update.run.failure_code}</code></p> : null}
        </div>
        {children}
      </CardContent>
    </Card>
  )
}
