import { expect, type Page } from '@playwright/test'

import type { AdmissionSummary, AdmissionSummaryPage } from '../src/lib/types/company-admissions'
import { pendingAdmission, prepareAdmission } from './admission-withdraw-fixture'
import { originalToken, replacementToken } from './session-fencing-fixture'

export const historyPath = '/api/company-admissions/mine/page'
export const historyCursor = 'older/+?=&one'
export const historyRows: readonly AdmissionSummary[] = (['pending', 'approved', 'rejected', 'withdrawn'] as const).map((status, index) => ({
  ...pendingAdmission,
  id: `history-${status}`,
  status,
  target_tenant: { id: `target-${status}`, name: `History ${status} company` },
  created_at: `2026-10-${20 - index}T12:00:00Z`,
}))
export const replacementRow: AdmissionSummary = {
  ...pendingAdmission, id: 'replacement-history', target_tenant: { id: 'new-target', name: 'Replacement applicant company' },
}
export type HistoryCall = { readonly status: string | null; readonly cursor: string | null; readonly token: string | undefined }

export async function prepareHistory(page: Page) {
  const admission = await prepareAdmission(page)
  const calls: HistoryCall[] = []
  const pages = new Map<string, AdmissionSummaryPage>()
  await page.route(`**${historyPath}*`, async (route) => {
    const request = route.request()
    const query = new URL(request.url()).searchParams
    if (query.get('limit') === '100') return route.fallback()
    const token = request.headers().authorization
    const status = query.get('status')
    const cursor = query.get('cursor')
    expect(request.method()).toBe('GET')
    expect(request.postData()).toBeNull()
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(token)
    expect(query.get('limit')).toBe('50')
    expect(query.has('offset')).toBe(false)
    expect(status === null || ['pending', 'approved', 'rejected', 'withdrawn'].includes(status)).toBe(true)
    expect(cursor === null || cursor === historyCursor).toBe(true)
    calls.push({ status, cursor, token })
    const override = pages.get(`${status ?? 'all'}:${cursor ?? 'root'}`)
    if (override) return route.fulfill({ json: override })
    const rows = token === `Bearer ${replacementToken}` ? [replacementRow] : historyRows
    const items = rows.filter((row) => status === null || row.status === status)
    await route.fulfill({ json: {
      items: cursor === null ? items.slice(0, 2) : items.slice(2),
      next_cursor: cursor === null && items.length > 2 ? historyCursor : null,
    } })
  })
  return { ...admission, calls, pages }
}
