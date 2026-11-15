import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { useLocation, useParams, useSearchParams } from 'react-router-dom'
import { z } from 'zod'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { BackLink } from '@/components/ui/back-button'
import { resourcePressureSummaryOptions } from '@/hooks/resource-pressure-options'
import { useTranslation } from '@/hooks/use-translation'
import { ApiError } from '@/lib/http'
import type { ResourcePressureFilters } from '@/lib/resource-pressure-api'
import { UpgradeSignalsCompanySummary } from '@/pages/upgrade-signals-company-summary'
import { UpgradeSignalsEpisodes } from '@/pages/upgrade-signals-episodes'
import { readUpgradeSignalsForm, upgradeSignalsQuery } from '@/pages/upgrade-signals-filters'

export function UpgradeSignalsDetailPage() {
  const { t } = useTranslation()
  const { tenantId } = useParams<{ tenantId: string }>()
  const { search } = useLocation()
  const [params] = useSearchParams()
  const tenant = z.guid().safeParse(tenantId)
  const query = useMemo(() => upgradeSignalsQuery(readUpgradeSignalsForm(params)), [params])

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
      <BackLink to={`/upgrade-signals${search}`} label={t('Back to summary')} className="self-start" />
      {!tenant.success ? <p role="alert">{t('That company was not found.')}</p>
        : query.filters === null ? <p role="alert">{t('The To date must be on or after the From date.')}</p>
          : <CompanySignals key={JSON.stringify([tenant.data, query.filters])} filters={{ ...query.filters, tenantId: tenant.data }} tenantId={tenant.data} />}
    </div>
  )
}

function CompanySignals({ tenantId, filters }: { readonly tenantId: string; readonly filters: ResourcePressureFilters }) {
  const { t } = useTranslation()
  const summary = useQuery(resourcePressureSummaryOptions(filters))
  const entry = summary.data?.find((row) => row.tenant_id === tenantId)
  const company = entry?.tenant_name ?? t('Unknown company')
  const loadError = summary.error instanceof ApiError ? summary.error.message : t('Could not load upgrade signals. Check your connection and try again.')

  return (
    <>
      <h1 className="font-display text-2xl font-semibold tracking-tight [overflow-wrap:anywhere]">{entry ? t('Upgrade signals for {company}', { company }) : t('Upgrade signals')}</h1>
      {summary.isPending ? <p role="status">{t('Loading upgrade signals…')}</p>
        : summary.isError ? <div role="alert" className="flex flex-col items-start gap-3">
          <ErrorToast error={summary.error} message={loadError} />
          <p className="text-sm text-destructive">{loadError}</p>
          <Button variant="outline" onClick={() => void summary.refetch()}>{t('Try again')}</Button>
        </div>
          : <>
            {entry ? <UpgradeSignalsCompanySummary entry={entry} /> : <p role="status">{t('No resource pressure recorded in this period')}</p>}
            <UpgradeSignalsEpisodes tenantId={tenantId} company={company} filters={filters} />
          </>}
    </>
  )
}
