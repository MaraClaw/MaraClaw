import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { RefreshButton } from '@/components/ui/refresh-button'
import { companyRuntimeOptions } from '@/hooks/company-options'
import { useTranslation } from '@/hooks/use-translation'
import { ApiError } from '@/lib/http'
import { dateTimeFormat } from '@/lib/resource-pressure-format'
import { CompanyRuntimeRow } from './company-runtime-row'

export function CompanyRuntime({ companyId }: { readonly companyId: string }) {
  const { t, date } = useTranslation()
  const [cursors, setCursors] = useState<readonly (string | null)[]>([null])
  const cursor = cursors.at(-1) ?? null
  const runtime = useQuery(companyRuntimeOptions(companyId, cursor))
  return (
    <section aria-labelledby="runtime-inventory-heading" className="flex min-w-0 flex-col gap-4 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="runtime-inventory-heading" className="font-display text-lg font-semibold tracking-tight">{t('Runtime inventory')}</h2>
        <RefreshButton label={t('Refresh runtimes')} disabled={runtime.isFetching} onClick={() => { void runtime.refetch() }} />
      </div>
      {runtime.isFetching ? <p role="status" className="text-sm text-muted-foreground">{t('Loading runtime inventory...')}</p> : null}
      {runtime.error ? <p role="alert" className="text-sm text-destructive">{runtime.error instanceof ApiError ? runtime.error.message : t('Could not load runtime inventory. Refresh to retry.')}</p> : null}
      {runtime.data && !runtime.error ? <>
        <p className="text-sm text-muted-foreground">{t('Observed at {time}', { time: date(runtime.data.as_of, dateTimeFormat) })}</p>
        {!runtime.data.observation_available ? <p role="status" className="text-sm text-warning-foreground">{t('Docker observations are unavailable. Recorded status and diagnostics remain visible; this does not mean the guests are stopped.')}</p> : null}
        {runtime.data.items.length ? runtime.data.items.map(agent => <CompanyRuntimeRow key={agent.agent_id} agent={agent} />) : <p className="text-sm text-muted-foreground">{t('No agent runtimes on this page. Refresh to check for new agents.')}</p>}
      </> : null}
      <div className="flex flex-wrap gap-3">
        <Button type="button" variant="outline" disabled={cursors.length === 1 || runtime.isFetching} onClick={() => setCursors(previous => previous.slice(0, -1))}>{t('Previous page')}</Button>
        <Button type="button" variant="outline" disabled={!runtime.data?.next_cursor || runtime.isFetching || runtime.isError} onClick={() => {
          const next = runtime.data?.next_cursor
          if (next) setCursors(previous => [...previous, next])
        }}>{t('Next page')}</Button>
      </div>
    </section>
  )
}
