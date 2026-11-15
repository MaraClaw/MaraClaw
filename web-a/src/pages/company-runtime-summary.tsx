import { useQuery } from '@tanstack/react-query'
import { ArrowRight } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { companyRuntimeOptions } from '@/hooks/company-options'
import { useTranslation } from '@/hooks/use-translation'
import { ApiError } from '@/lib/http'
import { dateTimeFormat } from '@/lib/resource-pressure-format'

export function CompanyRuntimeSummary({ companyId }: { readonly companyId: string }) {
  const { t, number, date } = useTranslation()
  const runtime = useQuery(companyRuntimeOptions(companyId, null))
  return (
    <Card className="relative min-w-0 transition-colors hover:bg-muted/40 [overflow-wrap:anywhere]">
      <Link
        to={`/companies/${companyId}/runtimes`}
        aria-label={t('Runtime inventory')}
        className="absolute inset-0 z-10 rounded-[inherit] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      />
      <CardHeader>
        <CardTitle>{t('Runtime inventory')}</CardTitle>
        <CardDescription>{t('First page only. Open the inventory for all pages and diagnostics.')}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {runtime.isFetching ? <p role="status" className="text-sm text-muted-foreground">{t('Loading runtime inventory...')}</p> : null}
        {runtime.error ? <p role="alert" className="text-sm text-destructive">{runtime.error instanceof ApiError ? runtime.error.message : t('Could not load runtime summary. Open the inventory to retry.')}</p> : null}
        {runtime.data && !runtime.error ? <>
          <dl className="grid grid-cols-2 gap-4 text-sm">
            {[
              [t('Agents on first page'), number(runtime.data.items.length)],
              [t('Observed running'), runtime.data.observation_available ? number(runtime.data.items.filter(agent => agent.observation.state === 'observed' && agent.observation.running === true).length) : t('Unknown')],
              [t('With blocking codes'), number(runtime.data.items.filter(agent => agent.blocking_codes.length > 0).length)],
              [t('With runtime loss'), number(runtime.data.items.filter(agent => agent.runtime_loss !== null).length)],
            ].map(([label, value]) => <div key={label} className="min-w-0">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="mt-1 font-medium tabular-nums">{value}</dd>
            </div>)}
          </dl>
          <p className="text-sm text-muted-foreground">{t('Observed at {time}', { time: date(runtime.data.as_of, dateTimeFormat) })}</p>
          {!runtime.data.observation_available ? <p role="status" className="text-sm text-warning-foreground">{t('Docker observations are unavailable. Recorded status and diagnostics remain visible; this does not mean the guests are stopped.')}</p> : null}
          {runtime.data.next_cursor ? <p className="text-sm text-muted-foreground">{t('More runtime pages are available.')}</p> : null}
        </> : null}
        <p className="flex items-center gap-2 text-sm font-medium">{t('View runtime details')}<ArrowRight className="size-4" aria-hidden /></p>
      </CardContent>
    </Card>
  )
}
