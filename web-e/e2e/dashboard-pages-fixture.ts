import { expect, type Page } from '@playwright/test'
import { originalAgent, originalCompany, originalToken, originalUser, prepareWorkspace, replacementAgent, replacementToken, replacementUser } from './session-fencing-fixture'

export const currentPeriod = { start: '2026-10-01', end: '2026-12-31', label: 'Current quarter', is_current: true }
export const previousPeriod = { start: '2026-07-01', end: '2026-09-30', label: 'Previous quarter', is_current: false }
export const objectiveCursor = 'objective/+?='
export const relationshipCursor = 'relationship/+?='

export function objective(title: string, period = currentPeriod) {
  return { id: title, title, owner_type: 'company', status: 'active', period_start: period.start, period_end: period.end,
    created_at: '2026-10-01T00:00:00Z', key_results: [
      { id: `${title}-kr`, objective_id: title, title: `${title} result`, target_value: 100, current_value: 40,
        status: 'on_track', last_updated_at: '2026-10-01T00:00:00Z', created_at: '2026-10-01T00:00:00Z' },
    ] }
}

export function relationship(name: string) {
  return { id: name, member_id: name, relation: 'collaborator', relation_label: 'Collaborator', description: '',
    access_allowed: true, access_status: 'active', access_status_reason: null,
    member: { name, title: '', department_path: '', avatar_url: null, email: `${name}@fixture.test`, provider_name: null,
      provider_type: null, user_id: null, is_platform_user: false } }
}

export async function prepareDashboardPages(page: Page) {
  await prepareWorkspace(page)
  await page.route('**/api/member/auth/me', (route) => route.fulfill({ json: {
    ...originalUser, role: 'org_admin',
    ...(route.request().headers().authorization === `Bearer ${replacementToken}` ? { id: replacementUser.id } : {}),
  } }))
  await page.route('**/api/tenants/me', (route) => route.fulfill({ json: originalCompany }))
  for (const agent of [originalAgent, replacementAgent]) {
    await page.route(`**/api/agents/${agent.id}`, (route) => route.fulfill({ json: agent }))
  }
  const objectiveRequests: (string | null)[] = []
  const relationshipRequests: (string | null)[] = []
  let objectiveCreated = false
  let added = false
  let removed = false
  await page.route('**/api/okr/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(request.headers().authorization)
    if (url.pathname === '/api/okr/objectives' && request.method() === 'POST') {
      expect(request.postDataJSON()).toEqual({ title: 'Created objective', owner_type: 'company',
        period_start: currentPeriod.start, period_end: currentPeriod.end })
      objectiveCreated = true
      return route.fulfill({ json: objective('Created objective') })
    }
    expect(request.method()).toBe('GET')
    switch (url.pathname) {
      case '/api/okr/settings': return route.fulfill({ json: { enabled: false, period_frequency: 'quarterly',
        daily_report_enabled: false, daily_report_time: '18:00', weekly_report_enabled: false, weekly_report_day: 5 } })
      case '/api/okr/periods': return route.fulfill({ json: [previousPeriod, currentPeriod] })
      case '/api/okr/member-daily-reports':
      case '/api/okr/company-reports': return route.fulfill({ json: [] })
      case '/api/okr/objectives/page': {
        const cursor = url.searchParams.get('cursor')
        objectiveRequests.push(cursor)
        expect(url.searchParams.get('limit')).toBe('50')
        const period = url.searchParams.get('period_start') === previousPeriod.start ? previousPeriod : currentPeriod
        expect(url.searchParams.get('period_end')).toBe(period.end)
        const title = period === previousPeriod ? 'Previous objective' : cursor ? 'Second objective'
          : request.headers().authorization === `Bearer ${replacementToken}` ? 'Replacement objective'
          : objectiveCreated ? 'Created objective' : 'First objective'
        return route.fulfill({ json: { items: [objective(title, period)], next_cursor: cursor || period === previousPeriod ? null : objectiveCursor } })
      }
      default: throw new Error(`Unexpected dashboard API: ${request.method()} ${url.pathname}`)
    }
  })
  await page.route(/\/api\/agents\/[^/]+\/relationships(?:\/|\?|$)/, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(request.headers().authorization)
    if (url.pathname.endsWith('/member-candidates')) {
      expect(request.method()).toBe('GET')
      return route.fulfill({ json: [{ id: 'Added', name: 'Added', email: 'added@fixture.test' }] })
    }
    if (request.method() === 'POST') {
      expect(url.pathname).toBe(`/api/agents/${originalAgent.id}/relationships/`)
      expect(request.postDataJSON()).toEqual({ member_id: 'Added', relation: 'collaborator', description: '' })
      added = true
      return route.fulfill({ json: { status: 'ok' } })
    }
    if (request.method() === 'DELETE') {
      expect(url.pathname).toBe(`/api/agents/${originalAgent.id}/relationships/First`)
      removed = true
      return route.fulfill({ json: { status: 'ok' } })
    }
    expect(request.method()).toBe('GET')
    expect(url.pathname.endsWith('/page')).toBe(true)
    expect(url.searchParams.get('limit')).toBe('50')
    const cursor = url.searchParams.get('cursor')
    relationshipRequests.push(cursor)
    const items = cursor ? [relationship('Unseen'), ...(added ? [relationship('Added')] : [])]
      : removed ? [] : [relationship(url.pathname.includes(replacementAgent.id) || request.headers().authorization === `Bearer ${replacementToken}` ? 'Replacement person' : 'First')]
    return route.fulfill({ json: { items, next_cursor: cursor ? null : relationshipCursor, has_more: !cursor } })
  })
  return { objectiveRequests, relationshipRequests }
}
