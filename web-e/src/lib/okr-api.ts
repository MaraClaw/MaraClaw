import { apiRequest } from './http.ts'
import { z } from 'zod'
import { CompanyReportOutSchema, KeyResultOutSchema, MemberDailyReportOutSchema, ObjectiveOutSchema, OKRSettingsOutSchema, PeriodOutSchema } from './engine-contracts.generated.ts'
import type { CompanyReportRegenerate, KeyResultCreate, ObjectiveCreate, ObjectiveUpdate, OKRSettingsUpdate } from './engine-contracts.generated.ts'
export type { CompanyReportOut as CompanyReport, KeyResultOut as OkrKeyResult, MemberDailyReportOut as DailyReport, OKRSettingsOut as OkrSettings, PeriodOut as OkrPeriod } from './engine-contracts.generated.ts'
const objectiveSchema = ObjectiveOutSchema.extend({ key_results: z.array(KeyResultOutSchema).default([]) })
export type OkrObjective = z.infer<typeof objectiveSchema>

export type MemberWithoutOkr = {
  id: string
  type: string
  display_name: string
  source_label?: string | null
}

export async function getOkrSettings() {
  return apiRequest('/api/okr/settings', { schema: OKRSettingsOutSchema })
}

export async function updateOkrSettings(body: OKRSettingsUpdate) {
  return apiRequest('/api/okr/settings', { schema: OKRSettingsOutSchema, method: 'PUT', body })
}

export async function syncOkrRelationships(): Promise<{ status: string; okr_agent_id?: string }> {
  return apiRequest('/api/okr/sync-relationships', { method: 'POST' })
}

export async function listOkrPeriods() {
  return apiRequest('/api/okr/periods', { schema: z.array(PeriodOutSchema) })
}

export async function listObjectives(periodStart?: string, periodEnd?: string): Promise<OkrObjective[]> {
  const query = new URLSearchParams()
  if (periodStart) query.set('period_start', periodStart)
  if (periodEnd) query.set('period_end', periodEnd)
  const suffix = query.toString() ? `?${query}` : ''
  return apiRequest(`/api/okr/objectives${suffix}`, { schema: z.array(objectiveSchema) })
}

export async function createObjective(body: ObjectiveCreate): Promise<OkrObjective> {
  return apiRequest('/api/okr/objectives', { schema: objectiveSchema, method: 'POST', body })
}

export async function updateObjective(id: string, body: ObjectiveUpdate): Promise<OkrObjective> {
  return apiRequest(`/api/okr/objectives/${id}`, { schema: objectiveSchema, method: 'PATCH', body })
}

export async function createKeyResult(
  objectiveId: string,
  body: KeyResultCreate,
) {
  return apiRequest(`/api/okr/objectives/${objectiveId}/key-results`, { schema: KeyResultOutSchema, method: 'POST', body })
}

export async function updateKrProgress(krId: string, value: number, note?: string) {
  return apiRequest(`/api/okr/key-results/${krId}/progress`, { schema: KeyResultOutSchema, method: 'POST', body: { value, note } })
}

export async function listDailyReports(reportDate?: string) {
  const suffix = reportDate ? `?report_date=${reportDate}` : ''
  return apiRequest(`/api/okr/member-daily-reports${suffix}`, { schema: z.array(MemberDailyReportOutSchema) })
}

export async function submitDailyReport(body: { report_date: string; content: string }) {
  return apiRequest('/api/okr/member-daily-reports', { schema: MemberDailyReportOutSchema, method: 'POST', body })
}

export async function listCompanyReports() {
  return apiRequest('/api/okr/company-reports', { schema: z.array(CompanyReportOutSchema) })
}

export async function regenerateCompanyReport(body: CompanyReportRegenerate) {
  return apiRequest('/api/okr/company-reports/regenerate', { schema: CompanyReportOutSchema, method: 'POST', body })
}

export async function membersWithoutOkr(): Promise<{
  okr_agent_id?: string | null
  company_okr_exists?: boolean
  members_without_okr: MemberWithoutOkr[]
  total: number
}> {
  return apiRequest('/api/okr/members-without-okr')
}

export async function triggerOkrOutreach(): Promise<{ status: string }> {
  return apiRequest('/api/okr/trigger-member-outreach', { method: 'POST' })
}
