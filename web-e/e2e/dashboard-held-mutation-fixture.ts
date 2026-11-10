import { expect, type Page } from '@playwright/test'
import { currentPeriod, objective, objectiveCursor, prepareDashboardPages, relationship, relationshipCursor } from './dashboard-pages-fixture'
import { originalAgent, originalToken, replacementAgent, replacementToken } from './session-fencing-fixture'

export const writes = ['create', 'progress', 'key-result', 'add', 'remove'] as const
export type DashboardWrite = typeof writes[number]

export async function prepareHeldDashboardWrite(page: Page, write: DashboardWrite) {
  await prepareDashboardPages(page)
  await page.clock.setFixedTime(new Date('2026-11-10T12:00:00Z'))
  const isOkr = write === 'create' || write === 'progress' || write === 'key-result'
  const path = isOkr ? '/app/okr' : `/app/agents/${originalAgent.id}/relationships`
  const navigation = isOkr ? 'Objective pages' : 'Relationship pages'
  const first = isOkr ? 'First objective' : 'First'
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const completed = Promise.withResolvers<void>()
  const requests: { readonly cursor: string | null; readonly committed: boolean; readonly replacement: boolean }[] = []
  let committed = false
  const freshCursor = 'fresh/+?='
  const endpoint = isOkr ? '**/api/okr/objectives/page?**' : '**/api/agents/*/relationships/page?**'
  await page.route(endpoint, async (route) => {
    const request = route.request()
    expect(request.method()).toBe('GET')
    const url = new URL(request.url())
    if (isOkr && url.searchParams.get('period_start') !== currentPeriod.start) return route.fallback()
    if (!isOkr && url.pathname.includes(replacementAgent.id)) return route.fallback()
    const replacement = request.headers().authorization === `Bearer ${replacementToken}`
    expect(request.headers().authorization).toBe(`Bearer ${replacement ? replacementToken : originalToken}`)
    const cursor = url.searchParams.get('cursor')
    requests.push({ cursor, committed, replacement })
    expect(url.searchParams.get('limit')).toBe('50')
    expect(cursor).toBe(cursor ? (committed && !replacement ? freshCursor : isOkr ? objectiveCursor : relationshipCursor) : null)
    if (isOkr) {
      const item = objective(cursor ? 'Second objective' : replacement ? 'Replacement objective' : 'First objective')
      const updated = committed && !replacement
      return route.fulfill({ json: {
        items: [
          ...(updated && write === 'create' && !cursor ? [objective('Created objective')] : []),
          { ...item, key_results: [
            ...item.key_results.map((kr) => ({ ...kr, current_value: updated && write === 'progress' ? 75 : 40 })),
            ...(updated && write === 'key-result' && !cursor ? [{ ...item.key_results[0], id: 'new-kr', title: 'Added key result' }] : []),
          ] },
        ],
        next_cursor: cursor ? null : updated ? freshCursor : objectiveCursor,
      } })
    }
    const items = cursor ? [relationship('Unseen')]
      : replacement ? [relationship('Replacement person')]
      : committed && write === 'remove' ? []
      : [relationship('First'), ...(committed && write === 'add' ? [relationship('Added')] : [])]
    return route.fulfill({ json: { items, next_cursor: cursor ? null : committed && !replacement ? freshCursor : relationshipCursor, has_more: !cursor } })
  })
  const mutationPath = (() => {
    switch (write) {
      case 'create': return '/api/okr/objectives'
      case 'progress': return '/api/okr/key-results/First%20objective-kr/progress'
      case 'key-result': return '/api/okr/objectives/First%20objective/key-results'
      case 'add': return `/api/agents/${originalAgent.id}/relationships/`
      case 'remove': return `/api/agents/${originalAgent.id}/relationships/First`
      default: return write satisfies never
    }
  })()
  await page.route(`**${mutationPath}`, async (route) => {
    const request = route.request()
    expect(request.method()).toBe(write === 'remove' ? 'DELETE' : 'POST')
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    switch (write) {
      case 'create': expect(request.postDataJSON()).toEqual({ title: 'Created objective', owner_type: 'company', period_start: currentPeriod.start, period_end: currentPeriod.end }); break
      case 'progress': expect(request.postDataJSON()).toEqual({ value: 75 }); break
      case 'key-result': expect(request.postDataJSON()).toEqual({ title: 'Added key result', target_value: 100 }); break
      case 'add': expect(request.postDataJSON()).toEqual({ member_id: 'Added', relation: 'collaborator', description: '' }); break
      case 'remove': expect(request.postData()).toBe(null); break
      default: write satisfies never
    }
    requested.resolve()
    await release.promise
    committed = true
    try {
      await route.fulfill({ json: write === 'create' ? objective('Created objective')
        : write === 'progress' || write === 'key-result' ? { ...objective('First objective').key_results[0], current_value: 75 }
        : { status: 'ok' } })
    } finally { completed.resolve() }
  })
  async function submit() {
    switch (write) {
      case 'create':
        await page.getByLabel('New company objective').fill('Created objective')
        await page.getByRole('button', { name: 'Add', exact: true }).click()
        break
      case 'progress':
        page.once('dialog', (dialog) => dialog.accept('75'))
        await page.getByRole('button', { name: 'Update progress', exact: true }).click()
        break
      case 'key-result':
        await page.getByPlaceholder('Add a key result').fill('Added key result')
        await page.getByRole('button', { name: 'KR', exact: true }).click()
        break
      case 'add':
        await page.getByLabel('Choose a person').selectOption('Added')
        await page.getByRole('button', { name: 'Add', exact: true }).click()
        break
      case 'remove': await page.getByRole('button', { name: 'Remove', exact: true }).click(); break
      default: write satisfies never
    }
    await requested.promise
  }
  async function complete() {
    const response = page.waitForResponse((response) => new URL(response.url()).pathname === mutationPath)
    release.resolve()
    await completed.promise
    await (await response).finished()
    // Drain the response's promise continuations before navigating back. This is
    // an event-loop barrier, not a timed delay or a substitute for DOM assertions.
    await page.evaluate(() => new Promise<void>((resolve) => {
      const channel = new MessageChannel()
      channel.port1.onmessage = () => { channel.port1.close(); channel.port2.close(); resolve() }
      channel.port2.postMessage(null)
    }))
  }
  async function expectUpdated() {
    const nav = page.getByRole('navigation', { name: navigation })
    await expect(nav.getByRole('status')).toHaveText('Page 1')
    switch (write) {
      case 'create': await expect(page.getByText('Created objective', { exact: true })).toBeVisible(); break
      case 'progress': await expect(page.getByText('75/100', { exact: true })).toBeVisible(); break
      case 'key-result': await expect(page.getByText('Added key result', { exact: true })).toBeVisible(); break
      case 'add': await expect(page.getByRole('list', { name: 'Relationships', exact: true }).getByText('Added', { exact: true })).toBeVisible(); break
      case 'remove':
        await expect(page.getByText('No relationships yet.', { exact: true })).toBeVisible()
        await expect(page.getByRole('list', { name: 'Relationships', exact: true }).getByRole('listitem')).toHaveCount(0)
        break
      default: write satisfies never
    }
    if (isOkr) await expect(page.getByText('First objective result', { exact: true })).toBeVisible()
    await nav.getByRole('button', { name: 'Next' }).click()
    await expect(page.getByText(isOkr ? 'Second objective' : 'Unseen', { exact: true })).toBeVisible()
    expect(requests.filter((request) => request.committed && !request.replacement).map((request) => request.cursor)).toEqual([null, freshCursor])
  }
  return { path, navigation, first, isOkr, requests, submit, complete, expectUpdated, release: () => release.resolve() }
}
