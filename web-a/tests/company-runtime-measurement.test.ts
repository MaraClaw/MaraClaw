import assert from 'node:assert/strict'
import test from 'node:test'

import { CompanyRuntimeWorkspaceOutSchema } from '../src/lib/engine-contracts.generated.ts'
import { loadVietnamese, localeTag, translate, type Locale, type Translator } from '../src/lib/i18n.ts'
import { workspaceMeasurementStatuses } from '../src/pages/company-runtime-measurement.ts'

await loadVietnamese()

function formattersFor(locale: Locale) {
  const t: Translator = (key, ...values) => translate(locale, key, values[0])
  return { t, date: (value: Date | string, options?: Intl.DateTimeFormatOptions) => new Date(value).toLocaleString(localeTag[locale], { ...options, timeZone: 'UTC' }) }
}
const en = formattersFor('en')
const measured = '2026-11-15T03:00:00Z'
const failed = '2026-11-15T04:00:00Z'
const requested = '2026-11-15T05:00:00Z'
const workspace = (times: { measured_at: string | null; walk_failed_at?: string; remeasure_requested_at?: string }) => CompanyRuntimeWorkspaceOutSchema.parse({
  used_bytes: 0, limit_bytes: 1024 ** 3, over_since: null, walk_failed_at: null, remeasure_requested_at: null, ...times,
})

for (const [name, input, expected] of [
  ['no usage row', null, [['never_measured', 'Never measured']]],
  ['a measurement with nothing pending', workspace({ measured_at: measured }), [['succeeded', 'Last measurement succeeded']]],
  ['a walk that failed after the last measurement', workspace({ measured_at: measured, walk_failed_at: failed }), [
    ['walk_failed', 'Last measurement failed at Nov 15, 2026, 4:00 AM'],
  ]],
  ['a row that holds only a failed walk', workspace({ measured_at: null, walk_failed_at: failed }), [
    ['walk_failed', 'Last measurement failed at Nov 15, 2026, 4:00 AM'], ['never_measured', 'Never measured'],
  ]],
  ['an unanswered re-measure request', workspace({ measured_at: measured, remeasure_requested_at: requested }), [
    ['succeeded', 'Last measurement succeeded'], ['remeasure_requested', 'Re-measure requested at Nov 15, 2026, 5:00 AM'],
  ]],
  ['a failed walk and a request', workspace({ measured_at: measured, walk_failed_at: failed, remeasure_requested_at: requested }), [
    ['walk_failed', 'Last measurement failed at Nov 15, 2026, 4:00 AM'], ['remeasure_requested', 'Re-measure requested at Nov 15, 2026, 5:00 AM'],
  ]],
] as const) test(`${name} maps to its measurement statuses`, () => {
  // When
  const statuses = workspaceMeasurementStatuses(input, en)
  // Then
  assert.deepEqual(statuses.map(({ kind, label }) => [kind, label]), expected)
})

test('Vietnamese statuses keep the time in the local date format', () => {
  // Given
  const vi = formattersFor('vi')
  // When
  const statuses = workspaceMeasurementStatuses(workspace({ measured_at: null, walk_failed_at: failed, remeasure_requested_at: requested }), vi)
  // Then
  assert.deepEqual(statuses.map(({ label }) => label), [
    'Lần đo gần nhất thất bại lúc 04:00 15 thg 11, 2026', 'Chưa từng đo', 'Đã yêu cầu đo lại lúc 05:00 15 thg 11, 2026',
  ])
})
