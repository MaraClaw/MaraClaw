import type { ReactNode } from 'react'

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useTranslation } from '@/hooks/use-translation'
import type { CompanyRuntimePage } from '@/lib/companies-api'
import { dateTimeFormat, formatBytes } from '@/lib/resource-pressure-format'

type RuntimeAgent = CompanyRuntimePage['items'][number]
const observationLabels = {
  observed: 'Observed', missing: 'Missing container', foreign: 'Foreign container', unavailable: 'Observation unavailable',
} as const satisfies Record<RuntimeAgent['observation']['state'], string>

function RuntimeField({ label, children }: { readonly label: string; readonly children: ReactNode }) {
  return <div className="min-w-0"><dt className="text-muted-foreground">{label}</dt><dd className="mt-1 whitespace-pre-wrap">{children}</dd></div>
}

export function CompanyRuntimeRow({ agent }: { readonly agent: RuntimeAgent }) {
  const { t, number, date } = useTranslation()
  const observation = agent.observation
  const unknown = t('Unknown')
  const at = (value: string | null | undefined) => value ? date(value, dateTimeFormat) : t('None')
  const yesNo = (value: boolean | null | undefined) => value === null || value === undefined ? unknown : t(value ? 'Yes' : 'No')
  const bytes = (value: number) => value < 1024 ** 2 ? `${number(value)} B` : formatBytes(value, number)
  return (
    <Card role="article" aria-labelledby={`runtime-agent-${agent.agent_id}`} className="min-w-0 [overflow-wrap:anywhere]">
      <CardHeader>
        <CardTitle id={`runtime-agent-${agent.agent_id}`}>{agent.name}</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-x-6 gap-y-4 text-sm sm:grid-cols-2">
          <RuntimeField label={t('Agent ID')}>{agent.agent_id}</RuntimeField>
          <RuntimeField label={t('Creator ID')}>{agent.creator_id}</RuntimeField>
          <RuntimeField label={t('Recorded status')}>{agent.recorded_status}</RuntimeField>
          <RuntimeField label={t('Recorded container')}>{agent.recorded_container_id ?? t('None')}</RuntimeField>
          <RuntimeField label={t('Observation')}>{t(observationLabels[observation.state])}</RuntimeField>
          <RuntimeField label={t('Container status')}>{observation.container_status ?? unknown}</RuntimeField>
          <RuntimeField label={t('Running')}>{yesNo(observation.running)}</RuntimeField>
          <RuntimeField label={t('Started at')}>{at(observation.started_at)}</RuntimeField>
          <RuntimeField label={t('Finished at')}>{at(observation.finished_at)}</RuntimeField>
          <RuntimeField label={t('Exit code')}>{observation.exit_code === null || observation.exit_code === undefined ? unknown : number(observation.exit_code)}</RuntimeField>
          <RuntimeField label={t('Restarts since last start')}>{observation.restart_count_since_last_start === null || observation.restart_count_since_last_start === undefined ? unknown : number(observation.restart_count_since_last_start)}</RuntimeField>
          <RuntimeField label={t('OOM killed since last start')}>{yesNo(observation.oom_killed_since_last_start)}</RuntimeField>
          <RuntimeField label={t('Runtime loss reason')}>{agent.runtime_loss?.reason ?? t('None')}</RuntimeField>
          <RuntimeField label={t('Runtime loss detected')}>{at(agent.runtime_loss?.detected_at)}</RuntimeField>
          <RuntimeField label={t('Start operation')}>{agent.start?.operation_id ?? t('None')}</RuntimeField>
          <RuntimeField label={t('Start state')}>{agent.start?.state ?? t('None')}</RuntimeField>
          <RuntimeField label={t('Start attempts')}>{agent.start ? number(agent.start.attempts) : t('None')}</RuntimeField>
          <RuntimeField label={t('Last start attempt')}>{at(agent.start?.last_attempt_at)}</RuntimeField>
          <RuntimeField label={t('Next start retry')}>{at(agent.start?.next_retry_at)}</RuntimeField>
          <RuntimeField label={t('Last start failure')}>{agent.start?.last_failure_code ?? t('None')}</RuntimeField>
          <RuntimeField label={t('Upgrade in progress')}>{yesNo(agent.upgrade_in_progress)}</RuntimeField>
          <RuntimeField label={t('Blocking codes')}>{agent.blocking_codes.length ? agent.blocking_codes.join(', ') : t('None')}</RuntimeField>
          <RuntimeField label={t('Workspace used / limit')}>{agent.workspace ? `${bytes(agent.workspace.used_bytes)} / ${bytes(agent.workspace.limit_bytes)}` : unknown}</RuntimeField>
          <RuntimeField label={t('Workspace measured')}>{at(agent.workspace?.measured_at)}</RuntimeField>
          <RuntimeField label={t('Workspace over limit since')}>{at(agent.workspace?.over_since)}</RuntimeField>
        </dl>
      </CardContent>
    </Card>
  )
}
