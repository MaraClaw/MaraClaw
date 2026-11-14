import assert from 'node:assert/strict'
import test from 'node:test'

import { resourcePressureEpisodeParams, resourcePressureFiltersSchema, resourcePressureSummaryParams } from '../src/lib/resource-pressure-api.ts'
import {
  emptyUpgradeSignalsForm, hasUpgradeSignalsFilters, readUpgradeSignalsForm, upgradeSignalsQuery, writeUpgradeSignalsForm,
} from '../src/pages/upgrade-signals-filters.ts'
import { tenantId } from './resource-pressure-fixtures.ts'

const everything = { since: '2026-11-01', until: '2026-11-07', tier: 'max', resource: 'cpu', company: tenantId }

test('the URL round-trips every filter and an empty URL means no filters', () => {
  // When
  const params = writeUpgradeSignalsForm(everything)
  // Then
  assert.deepEqual([...params], Object.entries(everything))
  assert.deepEqual(readUpgradeSignalsForm(params), everything)
  assert.deepEqual(readUpgradeSignalsForm(new URLSearchParams()), emptyUpgradeSignalsForm)
  assert.equal(writeUpgradeSignalsForm(emptyUpgradeSignalsForm).size, 0)
  assert.equal(hasUpgradeSignalsFilters(emptyUpgradeSignalsForm), false)
  assert.equal(hasUpgradeSignalsFilters({ ...emptyUpgradeSignalsForm, resource: 'memory' }), true)
})

test('unsupported URL values are dropped instead of reaching the engine', () => {
  // Given
  const params = new URLSearchParams({
    since: '2026-02-31', until: '2026-1-1', tier: 'enterprise', resource: 'gpu', company: 'acme', tab: 'analytics',
  })
  // When / Then
  assert.deepEqual(readUpgradeSignalsForm(params), emptyUpgradeSignalsForm)
})

test('both dates are included: From is local midnight and the exclusive until is local midnight after To', () => {
  // When
  const query = upgradeSignalsQuery(everything)
  // Then
  assert.equal(query.rangeError, false)
  assert.deepEqual(query.filters, {
    since: new Date(2026, 10, 1).toISOString(),
    until: new Date(2026, 10, 8).toISOString(),
    tier: 'max', resource: 'cpu', tenantId,
  })
})

test('a single-day range spans exactly one local day and month ends roll over', () => {
  // When
  const day = upgradeSignalsQuery({ ...emptyUpgradeSignalsForm, since: '2026-11-30', until: '2026-11-30' })
  const yearEnd = upgradeSignalsQuery({ ...emptyUpgradeSignalsForm, until: '2026-12-31' })
  // Then
  assert.deepEqual(day.filters, { since: new Date(2026, 10, 30).toISOString(), until: new Date(2026, 11, 1).toISOString() })
  assert.deepEqual(yearEnd.filters, { until: new Date(2027, 0, 1).toISOString() })
})

test('a To date before the From date is a range error with no query', () => {
  // When
  const query = upgradeSignalsQuery({ ...emptyUpgradeSignalsForm, since: '2026-11-10', until: '2026-11-09' })
  // Then
  assert.deepEqual(query, { filters: null, rangeError: true })
})

test('filters become the exact summary query the engine expects', () => {
  // Given
  const query = upgradeSignalsQuery(everything)
  assert.ok(query.filters)
  // When
  const summary = resourcePressureSummaryParams(resourcePressureFiltersSchema.parse(query.filters))
  // Then
  assert.deepEqual([...summary], [
    ['since', new Date(2026, 10, 1).toISOString()], ['until', new Date(2026, 10, 8).toISOString()],
    ['tier', 'max'], ['resource', 'cpu'], ['tenant_id', tenantId],
  ])
})

test('no filters send no summary or episode parameters', () => {
  // Given
  const query = upgradeSignalsQuery(emptyUpgradeSignalsForm)
  assert.ok(query.filters)
  const parsed = resourcePressureFiltersSchema.parse(query.filters)
  // When / Then
  assert.equal(resourcePressureSummaryParams(parsed).size, 0)
  assert.equal(resourcePressureEpisodeParams(parsed).size, 0)
})
