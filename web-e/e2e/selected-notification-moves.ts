import { expect, test } from '@playwright/test'

import { inboxPath, notice, prepareNotifications, replacementNotice } from './notifications-fixture'
import { replacementToken, writeFromOtherTab } from './session-fencing-fixture'

const first = { ...notice('Announcement', 'broadcast'), id: '11111111-1111-4111-8111-111111111111', link: '/plaza?post=one' }
const second = { ...notice('Tool', 'autonomy_l2'), id: '22222222-2222-4222-8222-222222222222' }

export function registerSelectedNotificationMoveTests() {
  for (const action of ['archive', 'restore'] as const) {
    const label = action === 'archive' ? 'Archive selected' : 'Restore selected'
    const rows = [first, second].map((row) => ({ ...row, archived_at: action === 'restore' ? '2030-01-02T00:00:00Z' : null }))
    for (const width of [390, 1440]) for (const colorScheme of ['light', 'dark'] as const) {
      test.describe(`${action} selected ${width} ${colorScheme}`, () => {
        test.use({ viewport: { width, height: 900 }, colorScheme, isMobile: width === 390, hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, reducedMotion: 'reduce' })
        test('failed move retains selection; retry moves one batch without reading and returns focus', async ({ page }, testInfo) => {
          // Given
          const fixture = await prepareNotifications(page, rows)
          await page.goto(inboxPath)
          if (action === 'restore') await page.getByRole('button', { name: 'Archived', exact: true }).click()
          await page.getByLabel('Category', { exact: true }).selectOption('social')
          const checkbox = page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true })
          await checkbox.focus(); await page.keyboard.press('Space')
          fixture.failWrites(true)
          // When
          await page.getByRole('button', { name: label, exact: true }).click()
          // Then
          await expect(page.getByRole('alert')).toContainText('Unable to move selected notifications')
          await expect(checkbox).toBeChecked()
          await expect(page.getByText(`${action === 'archive' ? 1 : 0} unread in Social`, { exact: true })).toBeVisible()
          await page.screenshot({ path: testInfo.outputPath('selected-error.png'), fullPage: true })
          fixture.failWrites(false)
          await page.getByRole('button', { name: label, exact: true }).focus(); await page.keyboard.press('Enter')
          await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).toBeFocused()
          await expect(checkbox).toHaveCount(0)
          await expect(page.getByText('0 selected (up to 200 loaded notifications)', { exact: true })).toBeVisible()
          await expect(page.getByText(`${action === 'archive' ? 0 : 1} unread in Social`, { exact: true })).toBeVisible()
          expect(fixture.requests.filter((call) => call.method === 'POST').map((call) => call.path)).toEqual([`/api/notifications/${action}`, `/api/notifications/${action}`])
          await page.getByRole('button', { name: action === 'archive' ? 'Archived' : 'Active', exact: true }).click()
          await expect(page.getByRole('button', { name: 'Mark as read: Announcement', exact: true })).toBeVisible()
          await expect(page.getByRole('link', { name: 'Open notification: Announcement', exact: true })).toHaveAttribute('href', '/app/plaza?post=one')
          await page.getByLabel('Category', { exact: true }).selectOption('broadcast')
          await expect(page.getByText(`${action === 'archive' ? 0 : 1} unread in Broadcasts`, { exact: true })).toBeVisible()
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
          await page.screenshot({ path: testInfo.outputPath('selected-moved.png'), fullPage: true })
        })
      })
    }

    test(`held selected ${action} snapshots IDs and preserves a newer category selection`, async ({ page }) => {
      // Given
      const fixture = await prepareNotifications(page, rows)
      const requested = Promise.withResolvers<void>(), release = Promise.withResolvers<void>()
      await page.route(`**/api/notifications/${action}`, async (route) => {
        expect(route.request().postDataJSON()).toEqual({ ids: [first.id] })
        requested.resolve(); await release.promise; await route.fallback()
      })
      try {
        await page.goto(inboxPath)
        if (action === 'restore') await page.getByRole('button', { name: 'Archived', exact: true }).click()
        await page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true }).check()
        await page.getByRole('button', { name: label, exact: true }).click(); await requested.promise
        await expect(page.getByRole('button', { name: 'Mark selected read', exact: true })).toBeDisabled()
        await expect(page.getByRole('button', { name: `${action === 'archive' ? 'Archive' : 'Restore'} notification: Tool`, exact: true })).toBeDisabled()
        // When
        await page.getByLabel('Category', { exact: true }).selectOption('tool')
        const newer = page.getByRole('checkbox', { name: 'Select notification: Tool', exact: true })
        await newer.check(); release.resolve()
        // Then
        await expect(page.getByRole('button', { name: label, exact: true })).toBeEnabled()
        await expect(newer).toBeChecked()
        await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).not.toBeFocused()
        expect(fixture.requests.filter((call) => call.method === 'POST')).toHaveLength(1)
      } finally { release.resolve() }
    })

    for (const status of [200, 503]) {
      test(`retired selected ${action} ${status} cannot alter replacement selection or focus`, async ({ page }) => {
        // Given
        const fixture = await prepareNotifications(page, rows)
        const requested = Promise.withResolvers<void>(), release = Promise.withResolvers<void>()
        await page.route(`**/api/notifications/${action}`, async (route) => {
          requested.resolve(); await release.promise
          await route.fulfill({ status, json: status === 200 ? { ok: true } : { detail: 'Obsolete failure' } })
        })
        try {
          await page.goto(inboxPath)
          if (action === 'restore') await page.getByRole('button', { name: 'Archived', exact: true }).click()
          await page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true }).check()
          await page.getByRole('button', { name: label, exact: true }).click(); await requested.promise
          await writeFromOtherTab(page, replacementToken)
          const replacement = page.getByRole('checkbox', { name: `Select notification: ${replacementNotice.title}`, exact: true })
          await replacement.check(); await replacement.focus()
          const before = fixture.requests.length
          const completed = page.waitForEvent('requestfinished', { predicate: (request) => request.method() === 'POST' && request.url().endsWith(`/${action}`) })
          // When
          release.resolve(); await completed
          // Then
          await expect(replacement).toBeChecked(); await expect(replacement).toBeFocused()
          await expect(page.getByRole('alert')).toHaveCount(0)
          expect(fixture.requests.slice(before)).toEqual([])
        } finally { release.resolve() }
      })
    }
  }
}
