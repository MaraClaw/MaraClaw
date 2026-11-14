import type { Translator } from './i18n.ts'
import type { PressureKind, PressureResource } from './engine-contracts.generated.ts'
import type { ResourcePressureEpisode, ResourcePressureSummary } from './resource-pressure-api.ts'
import type { PlainMessageKey } from './translations.ts'

export type NumberFormatter = (value: number, options?: Intl.NumberFormatOptions) => string
export type DateFormatter = (value: Date | string, options?: Intl.DateTimeFormatOptions) => string
export type Formatters = {
  readonly t: Translator
  readonly number: NumberFormatter
  readonly date: DateFormatter
}

export const dateTimeFormat = { dateStyle: 'medium', timeStyle: 'short' } as const satisfies Intl.DateTimeFormatOptions

const GIB = 1024 ** 3
const MIB = 1024 ** 2
const durationUnits = [['day', 86_400], ['hour', 3_600], ['minute', 60], ['second', 1]] as const

export const resourceLabels = {
  memory: 'Memory',
  cpu: 'CPU',
  workspace_disk: 'Workspace disk',
  managed_storage: 'Managed storage',
} as const satisfies Record<PressureResource, PlainMessageKey>

export const kindLabels = {
  threshold: 'Above threshold',
  oom_kill: 'OOM kill',
} as const satisfies Record<PressureKind, PlainMessageKey>

export function formatBytes(bytes: number, number: NumberFormatter): string {
  return bytes >= GIB
    ? `${number(bytes / GIB, { maximumFractionDigits: 2 })} GiB`
    : `${number(bytes / MIB, { maximumFractionDigits: 1 })} MiB`
}

export function formatMillicpus(millicpus: number, number: NumberFormatter): string {
  return `${number(millicpus / 1000, { maximumFractionDigits: 2 })} vCPU`
}

export function formatResourceValue(resource: PressureResource, value: number, number: NumberFormatter): string {
  return resource === 'cpu' ? formatMillicpus(value, number) : formatBytes(value, number)
}

export function formatRatio(ratio: number, number: NumberFormatter): string {
  return number(ratio, { style: 'percent', maximumFractionDigits: 1 })
}

/** The two largest non-zero units, e.g. "2h 5m"; anything under a second reads "0s". */
export function formatDuration(totalSeconds: number, number: NumberFormatter): string {
  let remaining = Math.max(0, Math.floor(totalSeconds))
  const parts: string[] = []
  for (const [unit, size] of durationUnits) {
    if (parts.length === 2) break
    const count = Math.floor(remaining / size)
    remaining -= count * size
    if (count > 0) parts.push(number(count, { style: 'unit', unit, unitDisplay: 'narrow' }))
  }
  return parts.length > 0 ? parts.join(' ') : number(0, { style: 'unit', unit: 'second', unitDisplay: 'narrow' })
}

/** Tier ids are product names, shown the way the organization admission screens show them. */
export function tierName(tier: string): string {
  return tier.toUpperCase()
}

export function summaryRowView(row: ResourcePressureSummary, { t, number, date }: Formatters) {
  return {
    id: row.tenant_id,
    company: row.tenant_name,
    currentTier: row.current_tier === null ? t('Not set') : tierName(row.current_tier),
    suggestedTier: row.suggested_tier === null ? t('None') : tierName(row.suggested_tier),
    hasSuggestion: row.suggested_tier !== null,
    memory: number(row.episode_counts.memory),
    cpu: number(row.episode_counts.cpu),
    workspaceDisk: number(row.episode_counts.workspace_disk),
    managedStorage: number(row.episode_counts.managed_storage),
    oomKills: number(row.oom_count),
    pressureTime: formatDuration(row.pressure_seconds, number),
    maxPeak: formatRatio(row.max_peak_ratio, number),
    agentsAffected: number(row.agents_affected),
    lastSeen: date(row.last_seen_at, dateTimeFormat),
    lastSeenAt: row.last_seen_at,
  }
}

export function episodeRowView(episode: ResourcePressureEpisode, { t, number, date }: Formatters) {
  return {
    id: episode.id,
    agent: episode.agent_name,
    agentRemoved: episode.agent_id === null,
    resource: t(resourceLabels[episode.resource]),
    kind: t(kindLabels[episode.kind]),
    tier: tierName(episode.tier),
    peak: formatResourceValue(episode.resource, episode.peak_value, number),
    limit: formatResourceValue(episode.resource, episode.limit_value, number),
    peakRatio: formatRatio(episode.peak_ratio, number),
    threshold: formatRatio(episode.threshold_ratio, number),
    started: date(episode.started_at, dateTimeFormat),
    startedAt: episode.started_at,
    lastSeen: date(episode.last_seen_at, dateTimeFormat),
    lastSeenAt: episode.last_seen_at,
    ended: episode.ended_at === null ? t('Still open') : date(episode.ended_at, dateTimeFormat),
    endedAt: episode.ended_at,
    samples: number(episode.sample_count),
  }
}
