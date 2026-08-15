import { apiRequest } from '@/lib/http'

export type OkrSettings = {
  enabled: boolean
  first_enabled_at?: string | null
  daily_report_enabled: boolean
  daily_report_time: string
  daily_report_skip_non_workdays: boolean
  period_frequency: string
  period_length_days?: number | null
  period_frequency_locked: boolean
  okr_agent_id?: string | null
}

export type OkrPeriod = {
  start: string
  end: string
  label: string
  is_current: boolean
}

export type OkrKeyResult = {
  id: string
  objective_id: string
  title: string
  target_value: number
  current_value: number
  unit?: string | null
  status: string
}

export type OkrObjective = {
  id: string
  title: string
  description?: string | null
  owner_type: string
  owner_id?: string | null
  owner_name?: string | null
  period_start: string
  period_end: string
  status: string
  key_results: OkrKeyResult[]
}

export type DailyReport = {
  id: string
  member_type: string
  member_id: string
  display_name: string
  report_date: string
  content: string
  status: string
}

export type CompanyReport = {
  id: string
  report_type: string
  period_start: string
  period_end: string
  period_label: string
  content: string
  submitted_count: number
  missing_count: number
  needs_refresh: boolean
}

export type MemberWithoutOkr = {
  id: string
  type: string
  display_name: string
  source_label?: string | null
}

export async function getOkrSettings(): Promise<OkrSettings> {
  return apiRequest('/api/okr/settings')
}

export async function updateOkrSettings(body: Partial<OkrSettings>): Promise<OkrSettings> {
  return apiRequest('/api/okr/settings', { method: 'PUT', body })
}

export async function syncOkrRelationships(): Promise<{ status: string; okr_agent_id?: string }> {
  return apiRequest('/api/okr/sync-relationships', { method: 'POST' })
}

export async function listOkrPeriods(): Promise<OkrPeriod[]> {
  return apiRequest('/api/okr/periods')
}

export async function listObjectives(periodStart?: string, periodEnd?: string): Promise<OkrObjective[]> {
  const query = new URLSearchParams()
  if (periodStart) query.set('period_start', periodStart)
  if (periodEnd) query.set('period_end', periodEnd)
  const suffix = query.toString() ? `?${query}` : ''
  return apiRequest(`/api/okr/objectives${suffix}`)
}

export async function createObjective(body: {
  title: string
  description?: string
  owner_type?: string
  owner_id?: string
  period_start: string
  period_end: string
}): Promise<OkrObjective> {
  return apiRequest('/api/okr/objectives', { method: 'POST', body })
}

export async function updateObjective(id: string, body: { title?: string; status?: string }): Promise<OkrObjective> {
  return apiRequest(`/api/okr/objectives/${id}`, { method: 'PATCH', body })
}

export async function createKeyResult(
  objectiveId: string,
  body: { title: string; target_value: number; unit?: string },
): Promise<OkrKeyResult> {
  return apiRequest(`/api/okr/objectives/${objectiveId}/key-results`, { method: 'POST', body })
}

export async function updateKrProgress(krId: string, value: number, note?: string): Promise<OkrKeyResult> {
  return apiRequest(`/api/okr/key-results/${krId}/progress`, { method: 'POST', body: { value, note } })
}

export async function listDailyReports(reportDate?: string): Promise<DailyReport[]> {
  const suffix = reportDate ? `?report_date=${reportDate}` : ''
  return apiRequest(`/api/okr/member-daily-reports${suffix}`)
}

export async function submitDailyReport(body: { report_date: string; content: string }): Promise<DailyReport> {
  return apiRequest('/api/okr/member-daily-reports', { method: 'POST', body })
}

export async function listCompanyReports(): Promise<CompanyReport[]> {
  return apiRequest('/api/okr/company-reports')
}

export async function regenerateCompanyReport(body: {
  report_type: string
  period_start: string
}): Promise<CompanyReport> {
  return apiRequest('/api/okr/company-reports/regenerate', { method: 'POST', body })
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
