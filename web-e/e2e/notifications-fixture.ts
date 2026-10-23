import { expect, type Page } from '@playwright/test'

import { notificationCategories, type NotificationCategory, type NotificationRow } from '../src/lib/notifications-api'
import { prepareWorkspace, replacementToken } from './session-fencing-fixture'

export const inboxPath = '/app/notifications'
export const replacementNotice: NotificationRow = { id: 'replacement', title: 'Replacement company notification', type: 'broadcast', is_read: false }
const categoryTypes = {
  all: [], tool: ['autonomy_l2'],
  approval: ['approval_pending', 'approval_resolved', 'company_admission_pending', 'company_admission_resolved'],
  social: ['plaza_comment', 'plaza_reply', 'mention', 'broadcast'], broadcast: ['broadcast'],
} satisfies Record<NotificationCategory, readonly string[]>

export function notice(id: string, type: string, is_read = false): NotificationRow {
  return { id, title: id, type, is_read, created_at: '2030-01-01T00:00:00Z' }
}

function matches(row: NotificationRow, category: NotificationCategory) {
  return category === 'all' || categoryTypes[category].some((type) => type === row.type)
}

export async function prepareNotifications(page: Page, initial: readonly NotificationRow[] = []) {
  await prepareWorkspace(page)
  let rows = initial
  let failWrites = false
  let failLists = false
  let failCounts = false
  const requests: { readonly path: string; readonly method: string; readonly category: string; readonly offset: number; readonly limit: number }[] = []
  await page.route('**/api/notifications**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const category = notificationCategories.find((value) => value === url.searchParams.get('category')) ?? 'all'
    const offset = Number(url.searchParams.get('offset') ?? 0)
    const limit = Number(url.searchParams.get('limit') ?? 50)
    requests.push({ path: url.pathname, method: request.method(), category, offset, limit })
    if (request.method() === 'POST') {
      if (failWrites) return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
      rows = rows.map((row) => ({ ...row, is_read: row.is_read || (url.pathname.endsWith('/read-all') ? matches(row, category) : url.pathname.endsWith(`/${row.id}/read`)) }))
      return route.fulfill({ status: 204 })
    }
    const current = request.headers().authorization === `Bearer ${replacementToken}` ? [replacementNotice] : rows
    const filtered = current.filter((row) => matches(row, category))
    if (url.pathname.endsWith('/unread-count')) {
      if (failCounts) return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
      return route.fulfill({ json: { count: filtered.filter((row) => !row.is_read).length } })
    }
    expect(limit).toBeLessThanOrEqual(200)
    if (failLists) return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
    return route.fulfill({ json: filtered.slice(offset, offset + limit) })
  })
  return {
    requests,
    failWrites: (value: boolean) => { failWrites = value },
    failLists: (value: boolean) => { failLists = value },
    failCounts: (value: boolean) => { failCounts = value },
  }
}
