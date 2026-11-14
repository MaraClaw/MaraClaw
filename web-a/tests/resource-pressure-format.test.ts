import assert from 'node:assert/strict'
import test from 'node:test'

import { localeTag, translate, type Locale, type Translator } from '../src/lib/i18n.ts'
import {
  dateTimeFormat, episodeRowView, formatBytes, formatDuration, formatMillicpus, formatRatio, formatResourceValue, kindLabels, resourceLabels, summaryRowView,
} from '../src/lib/resource-pressure-format.ts'
import { ResourcePressureEpisodeOutSchema, ResourcePressureTenantSummarySchema } from '../src/lib/engine-contracts.generated.ts'
import { vietnamese } from '../src/lib/translations.ts'
import { cpuEpisodeRow, episodeRow, quietSummaryRow, summaryRow } from './resource-pressure-fixtures.ts'

function formattersFor(locale: Locale) {
  const t: Translator = (key, ...values) => translate(locale, key, values[0])
  return {
    t,
    number: (value: number, options?: Intl.NumberFormatOptions) => new Intl.NumberFormat(localeTag[locale], options).format(value),
    date: (value: Date | string, options?: Intl.DateTimeFormatOptions) => new Date(value).toLocaleString(localeTag[locale], { ...options, timeZone: 'UTC' }),
  }
}
const en = formattersFor('en')
const vi = formattersFor('vi')
const summary = ResourcePressureTenantSummarySchema.parse(summaryRow)
const quietSummary = ResourcePressureTenantSummarySchema.parse(quietSummaryRow)
const episode = ResourcePressureEpisodeOutSchema.parse(episodeRow)
const cpuEpisode = ResourcePressureEpisodeOutSchema.parse(cpuEpisodeRow)

test('bytes read as GiB from one GiB up and as MiB below it', () => {
  assert.equal(formatBytes(2 * 1024 ** 3, en.number), '2 GiB')
  assert.equal(formatBytes(1.5 * 1024 ** 3, en.number), '1.5 GiB')
  assert.equal(formatBytes(1024 ** 3, en.number), '1 GiB')
  assert.equal(formatBytes(512 * 1024 ** 2, en.number), '512 MiB')
  assert.equal(formatBytes(0, en.number), '0 MiB')
})

test('millicpus read as vCPU', () => {
  assert.equal(formatMillicpus(2000, en.number), '2 vCPU')
  assert.equal(formatMillicpus(500, en.number), '0.5 vCPU')
  assert.equal(formatMillicpus(1250, en.number), '1.25 vCPU')
})

test('resource values pick the unit from the resource', () => {
  assert.equal(formatResourceValue('cpu', 1900, en.number), '1.9 vCPU')
  for (const resource of ['memory', 'workspace_disk', 'managed_storage'] as const) {
    assert.equal(formatResourceValue(resource, 4 * 1024 ** 3, en.number), '4 GiB')
  }
})

test('ratios read as percentages with at most one decimal', () => {
  assert.equal(formatRatio(0.9734, en.number), '97.3%')
  assert.equal(formatRatio(0.9, en.number), '90%')
  assert.equal(formatRatio(1.0004, en.number), '100%')
})

for (const [seconds, expected] of [
  [0, '0s'], [0.9, '0s'], [45, '45s'], [60, '1m'], [3_661, '1h 1m'], [7_500, '2h 5m'], [86_400, '1d'],
  [90_061, '1d 1h'], [-5, '0s'],
] as const) test(`duration of ${seconds} seconds reads ${expected}`, () => {
  assert.equal(formatDuration(seconds, en.number), expected)
})

test('a summary row presents every column with a prominent suggestion', () => {
  // When
  const row = summaryRowView(summary, en)
  // Then
  assert.deepEqual(row, {
    id: summary.tenant_id, company: 'Acme Corp', currentTier: 'PRO', suggestedTier: 'MAX', hasSuggestion: true,
    memory: '4', cpu: '1', workspaceDisk: '0', managedStorage: '2', oomKills: '1',
    pressureTime: '2h 5m', maxPeak: '97.3%', agentsAffected: '3',
    lastSeen: en.date('2026-11-12T08:30:00Z', dateTimeFormat), lastSeenAt: '2026-11-12T08:30:00Z',
  })
  assert.match(row.lastSeen, /^Nov 12, 2026, 8:30\s?AM$/)
})

test('a null suggestion reads None and a null current tier reads Not set', () => {
  // When
  const row = summaryRowView(quietSummary, en)
  // Then
  assert.equal(row.suggestedTier, 'None')
  assert.equal(row.hasSuggestion, false)
  assert.equal(row.currentTier, 'Not set')
  assert.equal(row.pressureTime, '0s')
  assert.equal(summaryRowView(quietSummary, vi).suggestedTier, vietnamese.None)
  assert.equal(summaryRowView(quietSummary, vi).currentTier, vietnamese['Not set'])
})

test('an open memory episode shows bytes against its limit and a still-open end', () => {
  // When
  const row = episodeRowView(episode, en)
  // Then
  assert.deepEqual(row, {
    id: episode.id, agent: 'Sales assistant', agentRemoved: false, resource: 'Memory', kind: 'Above threshold', tier: 'PRO',
    peak: '1.85 GiB', limit: '2 GiB', peakRatio: '92.7%', threshold: '90%',
    started: en.date('2026-11-12T08:00:00Z', dateTimeFormat), startedAt: '2026-11-12T08:00:00Z',
    lastSeen: en.date('2026-11-12T08:30:00Z', dateTimeFormat), lastSeenAt: '2026-11-12T08:30:00Z',
    ended: 'Still open', endedAt: null, samples: '7',
  })
})

test('a closed CPU OOM episode shows vCPU, its end time and a removed agent', () => {
  // When
  const row = episodeRowView(cpuEpisode, en)
  // Then
  assert.equal(row.resource, 'CPU')
  assert.equal(row.kind, 'OOM kill')
  assert.equal(row.peak, '1.9 vCPU')
  assert.equal(row.limit, '2 vCPU')
  assert.equal(row.agentRemoved, true)
  assert.equal(row.ended, en.date('2026-11-12T09:00:00Z', dateTimeFormat))
  assert.equal(row.endedAt, '2026-11-12T09:00:00Z')
})

test('every resource and kind label has a Vietnamese translation', () => {
  for (const label of [...Object.values(resourceLabels), ...Object.values(kindLabels)]) {
    assert.ok(vietnamese[label].trim().length > 0, label)
  }
  assert.equal(episodeRowView(episode, vi).resource, vietnamese.Memory)
  assert.equal(episodeRowView(episode, vi).ended, vietnamese['Still open'])
})
