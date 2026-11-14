import { OrganizationPlanIdValues, PressureResourceValues } from '../lib/engine-contracts.generated.ts'
import type { ResourcePressureFilters } from '../lib/resource-pressure-api.ts'

/** URL state: every field is a string, empty meaning "no filter". Dates are `YYYY-MM-DD` in the operator's local time. */
export type UpgradeSignalsForm = {
  readonly since: string
  readonly until: string
  readonly tier: string
  readonly resource: string
  readonly company: string
}

export const emptyUpgradeSignalsForm: UpgradeSignalsForm = { since: '', until: '', tier: '', resource: '', company: '' }

const dayPattern = /^(\d{4})-(\d{2})-(\d{2})$/
const guidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** Local midnight at the start of `day` plus `offsetDays`, or null when `day` is not a real calendar date. */
function localDay(day: string, offsetDays = 0): Date | null {
  const match = dayPattern.exec(day)
  if (!match) return null
  const [year, month, date] = [Number(match[1]), Number(match[2]), Number(match[3])]
  const probe = new Date(0)
  probe.setFullYear(year, month - 1, date)
  probe.setHours(0, 0, 0, 0)
  if (probe.getFullYear() !== year || probe.getMonth() !== month - 1 || probe.getDate() !== date) return null
  probe.setDate(date + offsetDays)
  return probe
}

function validDay(value: string | null): string {
  return value !== null && localDay(value) !== null ? value : ''
}

export function readUpgradeSignalsForm(params: URLSearchParams): UpgradeSignalsForm {
  const tier = params.get('tier')
  const resource = params.get('resource')
  const company = params.get('company') ?? ''
  return {
    since: validDay(params.get('since')),
    until: validDay(params.get('until')),
    tier: OrganizationPlanIdValues.find((value) => value === tier) ?? '',
    resource: PressureResourceValues.find((value) => value === resource) ?? '',
    company: guidPattern.test(company) ? company : '',
  }
}

export function writeUpgradeSignalsForm(form: UpgradeSignalsForm): URLSearchParams {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(form)) if (value) params.set(key, value)
  return params
}

export type UpgradeSignalsQuery =
  | { readonly filters: ResourcePressureFilters; readonly rangeError: false }
  | { readonly filters: null; readonly rangeError: true }

/** The From and To dates are both included, so the engine's exclusive `until` is local midnight after To. */
export function upgradeSignalsQuery(form: UpgradeSignalsForm): UpgradeSignalsQuery {
  const since = localDay(form.since)
  const until = localDay(form.until, 1)
  if (since && until && until <= since) return { filters: null, rangeError: true }
  const tier = OrganizationPlanIdValues.find((value) => value === form.tier)
  const resource = PressureResourceValues.find((value) => value === form.resource)
  return {
    rangeError: false,
    filters: {
      ...(since ? { since: since.toISOString() } : {}),
      ...(until ? { until: until.toISOString() } : {}),
      ...(tier ? { tier } : {}),
      ...(resource ? { resource } : {}),
      ...(form.company ? { tenantId: form.company } : {}),
    },
  }
}

export function hasUpgradeSignalsFilters(form: UpgradeSignalsForm): boolean {
  return Object.values(form).some(Boolean)
}
