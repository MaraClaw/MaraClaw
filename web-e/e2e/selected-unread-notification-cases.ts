import { expect, test } from '@playwright/test'

import { inboxPath, notice, prepareNotifications, replacementNotice } from './notifications-fixture'
import { replacementToken, writeFromOtherTab } from './session-fencing-fixture'

const first = { ...notice('Announcement', 'broadcast', true), id: '11111111-1111-4111-8111-111111111111' }
const second = { ...notice('Other announcement', 'broadcast'), id: '22222222-2222-4222-8222-222222222222' }

export function registerSelectedUnreadNotificationTests() {
  for (const width of [390, 1440, 1920]) for (const theme of ['light', 'dark'] as const) {
    test.describe(`${width} ${theme} selected unread`, () => {
      test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, isMobile: width === 390,
        hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme: theme, reducedMotion: 'reduce' })
      test('active and archived selection preserves archive state, retries, counts and reloads', async ({ page }, testInfo) => {
        const archived = [{ ...first, id: '33333333-3333-4333-8333-333333333333', title: 'Saved announcement', archived_at: '2030-01-02T00:00:00Z' }]
        const fixture = await prepareNotifications(page, [first, second, ...archived])
        await page.goto(inboxPath)
        await page.getByLabel('Category', { exact: true }).selectOption('social')
        const selected = page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true })
        await selected.check()
        await page.getByRole('checkbox', { name: 'Select notification: Other announcement', exact: true }).check()
        fixture.failWrites(true)
        await page.getByRole('button', { name: 'Mark selected unread', exact: true }).click()
        await expect(page.getByRole('alert')).toContainText('Unable to mark selected notifications as unread')
        await expect(selected).toBeChecked()
        await expect(page.getByText('1 unread in Social', { exact: true })).toBeVisible()
        fixture.failWrites(false)
        await page.getByRole('button', { name: 'Mark selected unread', exact: true }).focus()
        await page.keyboard.press('Enter')
        await expect(selected).not.toBeChecked()
        await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).toBeFocused()
        await expect(page.getByText('2 unread in Social', { exact: true })).toBeVisible()
        await page.getByLabel('Category', { exact: true }).selectOption('broadcast')
        await expect(page.getByText('2 unread in Broadcasts', { exact: true })).toBeVisible()
        await page.getByRole('button', { name: 'Archived', exact: true }).click()
        await page.getByRole('checkbox', { name: 'Select notification: Saved announcement', exact: true }).check()
        await page.getByRole('button', { name: 'Mark selected unread', exact: true }).click()
        await expect(page.getByRole('button', { name: 'Mark as read: Saved announcement', exact: true })).toBeVisible()
        await expect(page.getByRole('button', { name: 'Restore notification: Saved announcement', exact: true })).toBeEnabled()
        await expect(page.getByText('2 unread in Broadcasts', { exact: true })).toBeVisible()
        await page.screenshot({ path: testInfo.outputPath('archived-unread.png'), fullPage: true })
        await page.reload()
        await expect(page.getByRole('button', { name: 'Mark as read: Announcement', exact: true })).toBeVisible()
        await expect(page.getByRole('link', { name: /^Inbox\s*,\s*2 unread$/ }).first()).toBeVisible()
        await page.getByRole('button', { name: 'Archived', exact: true }).click()
        await expect(page.getByRole('button', { name: 'Mark as read: Saved announcement', exact: true })).toBeVisible()
        await page.getByRole('button', { name: 'Restore notification: Saved announcement', exact: true }).click()
        await expect(page.getByText('3 unread in All', { exact: true })).toBeVisible()
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        await page.screenshot({ path: testInfo.outputPath('restored-unread.png'), fullPage: true })
      })
    })
  }

  test('pending unread snapshots IDs, blocks conflicts and preserves a newer category selection', async ({ page }) => {
    const fixture = await prepareNotifications(page, [first, { ...second, type: 'autonomy_l2' }])
    const started = Promise.withResolvers<void>(), release = Promise.withResolvers<void>()
    await page.route('**/api/notifications/unread-selected', async (route) => {
      expect(route.request().postDataJSON()).toEqual({ ids: [first.id] })
      started.resolve(); await release.promise; await route.fallback()
    })
    try {
      await page.goto(inboxPath)
      await page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true }).check()
      await page.getByRole('button', { name: 'Mark selected unread', exact: true }).click(); await started.promise
      await expect(page.getByRole('button', { name: 'Marking selected unread…', exact: true })).toBeDisabled()
      await expect(page.getByRole('button', { name: 'Mark selected read', exact: true })).toBeDisabled()
      await expect(page.getByRole('button', { name: 'Archive selected', exact: true })).toBeDisabled()
      await page.getByLabel('Category', { exact: true }).selectOption('tool')
      const newer = page.getByRole('checkbox', { name: 'Select notification: Other announcement', exact: true })
      await newer.check(); await newer.focus()
      release.resolve()
      await expect(page.getByRole('button', { name: 'Mark selected unread', exact: true })).toBeEnabled()
      await expect(newer).toBeChecked(); await expect(newer).toBeFocused()
      expect(fixture.requests.filter((request) => request.method === 'POST')).toHaveLength(1)
    } finally { release.resolve() }
  })

  test('selected unread after loading a continuation restarts the first page and clears selection', async ({ page }) => {
    const rows = Array.from({ length: 51 }, (_, index) => ({ ...notice(`Row ${index}`, 'broadcast', true), id: `44444444-4444-4444-8444-${String(index).padStart(12, '0')}` }))
    const fixture = await prepareNotifications(page, rows)
    await page.goto(inboxPath)
    await page.getByRole('button', { name: 'Load more', exact: true }).click()
    await expect(page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')).toHaveCount(51)
    await page.getByRole('checkbox', { name: 'Select notification: Row 50', exact: true }).check()
    await page.getByRole('button', { name: 'Mark selected unread', exact: true }).click()
    await expect(page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')).toHaveCount(50)
    await expect(page.getByText('0 selected (up to 200 loaded notifications)', { exact: true })).toBeVisible()
    await expect(page.getByText('1 unread in All', { exact: true })).toBeVisible()
    expect(fixture.requests.filter((request) => request.path.endsWith('/page')).at(-1)?.cursor).toBeNull()
    await page.getByRole('button', { name: 'Load more', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Mark as read: Row 50', exact: true })).toBeVisible()
  })

  for (const status of [200, 503]) test(`retired unread ${status} cannot change replacement selection, focus or errors`, async ({ page }) => {
    const fixture = await prepareNotifications(page, [first])
    const started = Promise.withResolvers<void>(), release = Promise.withResolvers<void>()
    await page.route('**/api/notifications/unread-selected', async (route) => {
      started.resolve(); await release.promise
      await route.fulfill({ status, json: status === 200 ? { ok: true } : { detail: 'Obsolete failure' } })
    })
    try {
      await page.goto(inboxPath)
      await page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true }).check()
      await page.getByRole('button', { name: 'Mark selected unread', exact: true }).click(); await started.promise
      await writeFromOtherTab(page, replacementToken)
      const newer = page.getByRole('checkbox', { name: `Select notification: ${replacementNotice.title}`, exact: true })
      await newer.check(); await newer.focus()
      const calls = fixture.requests.length
      const finished = page.waitForEvent('requestfinished', { predicate: (request) => request.url().endsWith('/unread-selected') })
      release.resolve(); await finished
      await expect(newer).toBeChecked(); await expect(newer).toBeFocused()
      await expect(page.getByRole('alert')).toHaveCount(0)
      expect(fixture.requests).toHaveLength(calls)
    } finally { release.resolve() }
  })
}
