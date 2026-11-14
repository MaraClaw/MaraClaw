import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery } from '@tanstack/react-query'
import { useId, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { resourcePressureEpisodesOptions } from '@/hooks/resource-pressure-options'
import { useTranslation } from '@/hooks/use-translation'
import { PressureKindValues } from '@/lib/engine-contracts.generated'
import { ApiError } from '@/lib/http'
import { episodeRowView, kindLabels } from '@/lib/resource-pressure-format'
import type { ResourcePressureFilters } from '@/lib/resource-pressure-api'
import type { PlainMessageKey } from '@/lib/translations'
import { UpgradeSignalsCsvButton } from '@/pages/upgrade-signals-csv-button'

const episodeFilterSchema = z.object({
  agent_id: z.string().trim().pipe(z.union([z.literal(''), z.guid('Enter a valid UUID or leave this blank.')])),
  kind: z.union([z.literal(''), z.enum(PressureKindValues)]),
  open_only: z.boolean(),
})
type EpisodeFilterValues = z.infer<typeof episodeFilterSchema>
const emptyEpisodeFilters: EpisodeFilterValues = { agent_id: '', kind: '', open_only: false }
const buttonClass = 'h-auto min-h-11 max-w-full whitespace-normal'
const pageButtonClass = `${buttonClass} aria-disabled:pointer-events-none aria-disabled:opacity-50`
const headings = [
  'Agent', 'Resource', 'Kind', 'Tier', 'Peak', 'Limit', 'Peak usage', 'Threshold', 'Started', 'Last seen', 'Ended', 'Samples',
] as const satisfies readonly PlainMessageKey[]

/** Episodes of one company, newest first. The parent remounts it whenever the summary filters change. */
export function UpgradeSignalsEpisodes({ tenantId, company, filters }: {
  readonly tenantId: string
  readonly company: string
  readonly filters: ResourcePressureFilters
}) {
  const { t, owned, number, date } = useTranslation()
  const id = useId()
  const [applied, setApplied] = useState(emptyEpisodeFilters)
  const [cursors, setCursors] = useState<readonly string[]>([])
  const form = useForm<z.input<typeof episodeFilterSchema>, unknown, EpisodeFilterValues>({
    resolver: zodResolver(episodeFilterSchema),
    defaultValues: emptyEpisodeFilters,
  })
  const scope = useMemo<ResourcePressureFilters>(() => {
    const kind = PressureKindValues.find((value) => value === applied.kind)
    return {
      ...filters,
      tenantId,
      ...(applied.agent_id ? { agentId: applied.agent_id } : {}),
      ...(kind ? { kind } : {}),
      ...(applied.open_only ? { openOnly: true } : {}),
    }
  }, [filters, tenantId, applied])
  const episodes = useQuery(resourcePressureEpisodesOptions(scope, cursors.at(-1) ?? null))
  const hasEpisodeFilters = applied.agent_id !== '' || applied.kind !== '' || applied.open_only
  const loadError = episodes.error instanceof ApiError ? episodes.error.message : t('Could not load episodes. Check your connection and try again.')
  const { errors } = form.formState

  function applyFilters(next: EpisodeFilterValues) {
    setApplied(next)
    setCursors([])
  }

  function clearFilters() {
    form.reset(emptyEpisodeFilters)
    applyFilters(emptyEpisodeFilters)
  }

  return (
    <section aria-labelledby={`${id}-title`} aria-busy={episodes.isFetching} className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h3 id={`${id}-title`} className="font-display text-lg font-semibold [overflow-wrap:anywhere]">
          {t('Episodes for {company}', { company })}
        </h3>
        <UpgradeSignalsCsvButton
          key={JSON.stringify(scope)}
          filters={scope}
          accessibleName={t('Download CSV for {company}', { company })}
        />
      </div>

      <form noValidate onSubmit={form.handleSubmit(applyFilters)} className="flex flex-col gap-4">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <div className="flex min-w-0 flex-col gap-2">
            <Label htmlFor={`${id}-agent`}>{t('Agent ID')}</Label>
            <Input
              id={`${id}-agent`}
              autoComplete="off"
              spellCheck={false}
              placeholder={t('UUID (optional)')}
              aria-invalid={Boolean(errors.agent_id)}
              aria-describedby={errors.agent_id ? `${id}-agent-error` : undefined}
              {...form.register('agent_id')}
            />
            {errors.agent_id ? <p id={`${id}-agent-error`} role="alert" className="text-sm text-destructive">{owned(errors.agent_id.message)}</p> : null}
          </div>
          <div className="flex min-w-0 flex-col gap-2">
            <Label htmlFor={`${id}-kind`}>{t('Kind')}</Label>
            <Select id={`${id}-kind`} {...form.register('kind')}>
              <option value="">{t('All kinds')}</option>
              {PressureKindValues.map((kind) => <option key={kind} value={kind}>{t(kindLabels[kind])}</option>)}
            </Select>
          </div>
          <label className="flex min-h-11 items-center gap-3 self-end text-sm">
            <input type="checkbox" className="size-5 accent-primary" {...form.register('open_only')} />
            {t('Open episodes only')}
          </label>
        </div>
        <div className="flex flex-wrap gap-3">
          <Button type="submit" className={buttonClass}>{t('Apply filters')}</Button>
          <Button type="button" variant="outline" className={buttonClass} onClick={clearFilters}>{t('Clear filters')}</Button>
        </div>
      </form>

      {episodes.isPending ? (
        <p role="status" className="text-sm text-muted-foreground">{t('Loading episodes…')}</p>
      ) : episodes.isError ? (
        <div role="alert" className="flex flex-col items-start gap-3">
          <ErrorToast error={episodes.error} message={loadError} />
          <p className="text-sm text-destructive">{loadError}</p>
          <Button type="button" variant="outline" className={buttonClass} onClick={() => void episodes.refetch()}>{t('Try again')}</Button>
        </div>
      ) : episodes.data.items.length === 0 ? (
        <p role="status" className="text-sm text-muted-foreground">
          {cursors.length > 0 || hasEpisodeFilters ? t('No episodes match these filters.') : t('No resource pressure recorded in this period')}
        </p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full min-w-[64rem] text-sm">
            <caption className="sr-only">{t('Episodes for {company}', { company })}</caption>
            <thead className="text-start text-xs text-muted-foreground">
              <tr>{headings.map((heading) => <th key={heading} scope="col" className="px-3 py-2.5 text-start font-medium">{t(heading)}</th>)}</tr>
            </thead>
            <tbody>
              {episodes.data.items.map((episode) => {
                const row = episodeRowView(episode, { t, number, date })
                return (
                  <tr key={row.id} className="border-t border-border align-top">
                    <th scope="row" className="min-w-36 break-words px-3 py-2.5 text-start font-medium">
                      {row.agent}
                      {row.agentRemoved ? <span className="block text-xs font-normal text-muted-foreground">{t('Agent removed')}</span> : null}
                    </th>
                    <td className="whitespace-nowrap px-3 py-2.5">{row.resource}</td>
                    <td className="whitespace-nowrap px-3 py-2.5">{row.kind}</td>
                    <td className="px-3 py-2.5">{row.tier}</td>
                    <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{row.peak}</td>
                    <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{row.limit}</td>
                    <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{row.peakRatio}</td>
                    <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{row.threshold}</td>
                    <td className="px-3 py-2.5"><time dateTime={row.startedAt}>{row.started}</time></td>
                    <td className="px-3 py-2.5"><time dateTime={row.lastSeenAt}>{row.lastSeen}</time></td>
                    <td className="px-3 py-2.5">{row.endedAt === null ? row.ended : <time dateTime={row.endedAt}>{row.ended}</time>}</td>
                    <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{row.samples}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <nav aria-label={t('Episode pages')} className="flex flex-wrap items-center gap-3">
        {/* Inactive paging controls stay focusable so keyboard focus survives page changes. */}
        <Button type="button" variant="outline" className={pageButtonClass} aria-disabled={cursors.length === 0} onClick={() => {
          if (cursors.length > 0) setCursors((previous) => previous.slice(0, -1))
        }}>{t('Back')}</Button>
        <Button type="button" variant="outline" className={pageButtonClass} aria-disabled={!episodes.isSuccess || episodes.isFetching || episodes.data.next_cursor === null} onClick={() => {
          const next = episodes.data?.next_cursor
          if (episodes.isSuccess && !episodes.isFetching && next) setCursors((previous) => [...previous, next])
        }}>{t('Older')}</Button>
        <p className="text-sm text-muted-foreground">{t('Page {page}', { page: number(cursors.length + 1) })}</p>
      </nav>
    </section>
  )
}
