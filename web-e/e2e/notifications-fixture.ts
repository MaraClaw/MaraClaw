import { expect, type Page } from '@playwright/test'

import { notificationCategories, NotificationKind, notificationViewSchema, type NotificationCategory, type NotificationRow } from '../src/lib/notifications-api'
import { originalToken, prepareWorkspace, replacementToken } from './session-fencing-fixture'

export const inboxPath = '/app/notifications'
export const replacementNotice: NotificationRow = { id: 'replacement', title: 'Replacement company notification', type: NotificationKind.Broadcast, is_read: false, archived_at: null }
const categoryTypes = {
  all: [], tool: [NotificationKind.AutonomyL2],
  approval: [NotificationKind.ApprovalPending, NotificationKind.ApprovalResolved, NotificationKind.CompanyAdmissionPending, NotificationKind.CompanyAdmissionResolved],
  social: [NotificationKind.PlazaComment, NotificationKind.PlazaReply, NotificationKind.Mention, NotificationKind.Broadcast], broadcast: [NotificationKind.Broadcast],
} satisfies Record<NotificationCategory, readonly NotificationKind[]>

export function notice(id: string, type: NotificationKind, is_read = false): NotificationRow {
  return { id, title: id, type, is_read, created_at: '2030-01-01T00:00:00Z', archived_at: null }
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
  const requests: { readonly path: string; readonly method: string; readonly category: string; readonly view: string; readonly offset: number; readonly limit: number }[] = []
  await page.route('**/api/notifications**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const category = notificationCategories.find((value) => value === url.searchParams.get('category')) ?? 'all'
    const offset = Number(url.searchParams.get('offset') ?? 0)
    const limit = Number(url.searchParams.get('limit') ?? 50)
    const view = notificationViewSchema.parse(url.searchParams.get('view') ?? 'active')
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(request.headers().authorization)
    requests.push({ path: url.pathname, method: request.method(), category, view, offset, limit })
    if (request.method() === 'POST') {
      if (failWrites) return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
      if (url.pathname.endsWith('/read-all')) {
        rows = rows.map((row) => ({ ...row, is_read: row.is_read || (row.archived_at === null && matches(row, category)) }))
        return route.fulfill({ status: 204 })
      }
      const [, id, action] = /^\/api\/notifications\/([^/]+)\/(read|archive|restore)$/.exec(url.pathname) ?? []
      expect(action, 'Unexpected notification write').toBeDefined()
      if (!rows.some((row) => row.id === decodeURIComponent(id ?? ''))) return route.fulfill({ status: 404, json: { detail: 'Not found' } })
      rows = rows.map((row) => {
        if (row.id !== decodeURIComponent(id ?? '')) return row
        switch (action) {
          case 'archive': return { ...row, archived_at: row.archived_at ?? '2030-01-02T00:00:00Z' }
          case 'restore': return { ...row, archived_at: null }
          case 'read': return { ...row, is_read: true }
          default: throw new Error(`Unsupported notification action ${action}`)
        }
      })
      return route.fulfill(action === 'read' ? { status: 204 } : { json: { ok: true } })
    }
    expect(request.method()).toBe('GET')
    const current = request.headers().authorization === `Bearer ${replacementToken}` ? [replacementNotice] : rows
    const filtered = current.filter((row) => matches(row, category))
    if (url.pathname.endsWith('/unread-count')) {
      if (failCounts) return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
      return route.fulfill({ json: { count: filtered.filter((row) => row.archived_at === null && !row.is_read).length } })
    }
    expect(url.pathname).toBe('/api/notifications')
    expect(limit).toBeLessThanOrEqual(200)
    if (failLists) return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
    return route.fulfill({ json: filtered.filter((row) => (row.archived_at === null) === (view === 'active')).slice(offset, offset + limit) })
  })
  return {
    requests,
    failWrites: (value: boolean) => { failWrites = value },
    failLists: (value: boolean) => { failLists = value },
    failCounts: (value: boolean) => { failCounts = value },
  }
}
