import type { CompanyRuntimeWorkspaceOut } from '../lib/engine-contracts.generated.ts'
import { dateTimeFormat, type Formatters } from '../lib/resource-pressure-format.ts'

export type WorkspaceMeasurementKind = 'walk_failed' | 'never_measured' | 'succeeded' | 'remeasure_requested'
export type WorkspaceMeasurementStatus = { readonly kind: WorkspaceMeasurementKind; readonly label: string }

/**
 * Every condition the quota worker recorded for one agent's workspace, the failure first. No usage row and a row that
 * holds only a failed walk (`measured_at` null) are both never measured. A failed walk or re-measure request stays set
 * until a measurement that started at or after it is stored, so a set time is still unanswered.
 */
export function workspaceMeasurementStatuses(
  workspace: CompanyRuntimeWorkspaceOut | null,
  { t, date }: Pick<Formatters, 't' | 'date'>,
): readonly WorkspaceMeasurementStatus[] {
  if (workspace === null) return [{ kind: 'never_measured', label: t('Never measured') }]
  const statuses: WorkspaceMeasurementStatus[] = []
  if (workspace.walk_failed_at !== null) {
    statuses.push({ kind: 'walk_failed', label: t('Last measurement failed at {time}', { time: date(workspace.walk_failed_at, dateTimeFormat) }) })
  }
  if (workspace.measured_at === null) statuses.push({ kind: 'never_measured', label: t('Never measured') })
  else if (workspace.walk_failed_at === null) statuses.push({ kind: 'succeeded', label: t('Last measurement succeeded') })
  if (workspace.remeasure_requested_at !== null) {
    statuses.push({ kind: 'remeasure_requested', label: t('Re-measure requested at {time}', { time: date(workspace.remeasure_requested_at, dateTimeFormat) }) })
  }
  return statuses
}
