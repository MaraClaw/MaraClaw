import { useQuery } from '@tanstack/react-query'
import { useId, useMemo, useState, type FormEvent } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { companyOptions } from '@/hooks/company-options'
import { resourcePressureSummaryOptions } from '@/hooks/resource-pressure-options'
import { useTranslation } from '@/hooks/use-translation'
import { OrganizationPlanIdValues, PressureResourceValues } from '@/lib/engine-contracts.generated'
import { ApiError } from '@/lib/http'
import type { ResourcePressureFilters } from '@/lib/resource-pressure-api'
import { resourceLabels, tierName } from '@/lib/resource-pressure-format'
import { UpgradeSignalsCompanySummary } from '@/pages/upgrade-signals-company-summary'
import { UpgradeSignalsCsvButton } from '@/pages/upgrade-signals-csv-button'
import {
  emptyUpgradeSignalsForm,
  hasUpgradeSignalsFilters,
  readUpgradeSignalsForm,
  upgradeSignalsQuery,
  writeUpgradeSignalsForm,
  type UpgradeSignalsForm,
} from '@/pages/upgrade-signals-filters'

const buttonClass = 'h-auto min-h-11 max-w-full whitespace-normal'

export function UpgradeSignalsPage() {
  const { t } = useTranslation()
  const [params, setParams] = useSearchParams()
  const applied = useMemo(() => readUpgradeSignalsForm(params), [params])
  const query = useMemo(() => upgradeSignalsQuery(applied), [applied])

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
      <header className="flex flex-col gap-2">
        <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Upgrade signals')}</h1>
        <p className="text-muted-foreground">{t('Companies whose agents keep hitting their tier limits. Use this to decide who should move to a higher tier.')}</p>
        <p className="text-sm text-muted-foreground">{t('An episode is one stretch of time above the pressure threshold. OOM kills are counted separately. Time under pressure is a lower bound: usage is sampled about every 5 minutes, so the real time may be longer.')}</p>
        <p className="text-sm text-muted-foreground">{t('Suggested tier is the smallest higher tier that keeps the peaks seen here below the pressure threshold. An OOM kill suggests at least the next tier. None means no upgrade is suggested: the company is on the top tier, no higher tier would fit, or its tier is not set.')}</p>
      </header>

      <UpgradeSignalsFilters applied={applied} onApply={(next) => setParams(writeUpgradeSignalsForm(next), { replace: true })} />

      {query.filters === null
        ? null
        : <UpgradeSignalsResults key={JSON.stringify(query.filters)} filters={query.filters} filtered={hasUpgradeSignalsFilters(applied)} />}
    </div>
  )
}

function UpgradeSignalsFilters({ applied, onApply }: {
  readonly applied: UpgradeSignalsForm
  readonly onApply: (form: UpgradeSignalsForm) => void
}) {
  const { t } = useTranslation()
  const id = useId()
  const companies = useQuery(companyOptions())
  const [draft, setDraft] = useState(applied)
  const [syncedFrom, setSyncedFrom] = useState(applied)
  if (applied !== syncedFrom) {
    setSyncedFrom(applied)
    setDraft(applied)
  }
  const rangeError = upgradeSignalsQuery(draft).rangeError
  const unknownCompany = Boolean(draft.company && !companies.isLoading && !(companies.data ?? []).some((company) => company.id === draft.company))
  const change = (patch: Partial<UpgradeSignalsForm>) => setDraft((previous) => ({ ...previous, ...patch }))
  const nothingToClear = !hasUpgradeSignalsFilters(draft) && !hasUpgradeSignalsFilters(applied)

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!rangeError) onApply(draft)
  }

  function clear() {
    if (nothingToClear) return
    setDraft(emptyUpgradeSignalsForm)
    onApply(emptyUpgradeSignalsForm)
  }

  return (
    <Card>
      <CardHeader><CardTitle>{t('Filters')}</CardTitle></CardHeader>
      <CardContent>
        <form noValidate onSubmit={submit} className="flex flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <div className="flex min-w-0 flex-col gap-2">
              <Label htmlFor={`${id}-since`}>{t('From')}</Label>
              <Input id={`${id}-since`} type="date" value={draft.since} max={draft.until || undefined} aria-describedby={`${id}-dates-help`} onChange={(event) => change({ since: event.target.value })} />
            </div>
            <div className="flex min-w-0 flex-col gap-2">
              <Label htmlFor={`${id}-until`}>{t('To')}</Label>
              <Input
                id={`${id}-until`}
                type="date"
                value={draft.until}
                min={draft.since || undefined}
                aria-invalid={rangeError}
                aria-describedby={`${id}-dates-help${rangeError ? ` ${id}-range-error` : ''}`}
                onChange={(event) => change({ until: event.target.value })}
              />
            </div>
            <div className="flex min-w-0 flex-col gap-2">
              <Label htmlFor={`${id}-tier`}>{t('Tier when recorded')}</Label>
              <Select id={`${id}-tier`} value={draft.tier} onChange={(event) => change({ tier: event.target.value })}>
                <option value="">{t('All tiers')}</option>
                {OrganizationPlanIdValues.map((tier) => <option key={tier} value={tier}>{tierName(tier)}</option>)}
              </Select>
            </div>
            <div className="flex min-w-0 flex-col gap-2">
              <Label htmlFor={`${id}-resource`}>{t('Resource')}</Label>
              <Select id={`${id}-resource`} value={draft.resource} onChange={(event) => change({ resource: event.target.value })}>
                <option value="">{t('All resources')}</option>
                {PressureResourceValues.map((resource) => <option key={resource} value={resource}>{t(resourceLabels[resource])}</option>)}
              </Select>
            </div>
            <div className="flex min-w-0 flex-col gap-2">
              <Label htmlFor={`${id}-company`}>{t('Company')}</Label>
              <Select id={`${id}-company`} value={draft.company} onChange={(event) => change({ company: event.target.value })}>
                <option value="">{t('All companies')}</option>
                {unknownCompany ? <option value={draft.company}>{t('Unknown company')}</option> : null}
                {(companies.data ?? []).map((company) => <option key={company.id} value={company.id}>{company.name}</option>)}
              </Select>
            </div>
          </div>
          <p id={`${id}-dates-help`} className="text-sm text-muted-foreground">{t('Both dates are included, in your local time.')}</p>
          {rangeError ? <p id={`${id}-range-error`} role="alert" className="text-sm text-destructive">{t('The To date must be on or after the From date.')}</p> : null}
          {companies.isError ? <ErrorToast error={companies.error} message={t('Could not load the company list. Check your connection and try again.')} /> : null}
          <div className="flex flex-wrap gap-3">
            <Button type="submit" className={buttonClass}>{t('Apply filters')}</Button>
            {/* Stays focusable when nothing is filtered, so keyboard focus is not lost after clearing. */}
            <Button
              type="button"
              variant="outline"
              className={`${buttonClass} aria-disabled:pointer-events-none aria-disabled:opacity-50`}
              aria-disabled={nothingToClear}
              onClick={clear}
            >
              {t('Clear filters')}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

function UpgradeSignalsResults({ filters, filtered }: { readonly filters: ResourcePressureFilters; readonly filtered: boolean }) {
  const { t, number } = useTranslation()
  const { search } = useLocation()
  const id = useId()
  const summary = useQuery(resourcePressureSummaryOptions(filters))
  const loadError = summary.error instanceof ApiError ? summary.error.message : t('Could not load upgrade signals. Check your connection and try again.')
  const rows = summary.data

  return (
    <section aria-labelledby={`${id}-title`} aria-busy={summary.isFetching} className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 id={`${id}-title`} className="font-display text-lg font-semibold">{t('Summary by company')}</h2>
          <p role="status" className="text-sm text-muted-foreground">
            {rows ? t('Companies: {count}. With a suggested upgrade: {suggested}.', {
              count: number(rows.length),
              suggested: number(rows.filter((row) => row.suggested_tier !== null).length),
            }) : ''}
          </p>
        </div>
        <div className="flex min-w-0 flex-col items-start gap-1">
          <UpgradeSignalsCsvButton key={JSON.stringify(filters)} filters={filters} />
          <p className="max-w-xs text-xs text-muted-foreground">{t('Includes every episode that matches the current filters, not only the rows shown.')}</p>
        </div>
      </div>

      {summary.isPending ? (
        <Card><CardContent role="status">{t('Loading upgrade signals…')}</CardContent></Card>
      ) : summary.isError ? (
        <Card><CardContent role="alert" className="flex flex-col items-start gap-3">
          <ErrorToast error={summary.error} message={loadError} />
          <p className="text-sm text-destructive">{loadError}</p>
          <Button type="button" variant="outline" className={buttonClass} onClick={() => void summary.refetch()}>{t('Try again')}</Button>
        </CardContent></Card>
      ) : summary.data.length === 0 ? (
        <Card><CardContent className="flex flex-col gap-2">
          <p role="status">{t('No resource pressure recorded in this period')}</p>
          {filtered ? <p className="text-sm text-muted-foreground">{t('Widen the dates or clear the filters to see more.')}</p> : null}
        </CardContent></Card>
      ) : (
        <ul className="grid gap-4">
          {summary.data.map((entry) => (
            <li key={entry.tenant_id}>
              <UpgradeSignalsCompanySummary entry={entry} to={`/upgrade-signals/${entry.tenant_id}${search}`} />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
