import { expect, test } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

import { inboxPath, notice, prepareNotifications } from './notifications-fixture'

for (const width of [390, 1440, 1920]) {
  for (const theme of ['light', 'dark'] as const) {
    test.describe(`${width} ${theme} cursor matrix`, () => {
      test.use({
        viewport: { width, height: width === 390 ? 844 : 900 }, isMobile: width === 390,
        hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1,
        colorScheme: theme, reducedMotion: 'reduce',
      })
      test(`${width} ${theme} cursor rows dedupe and read mutation restarts the live traversal`, async ({ page }, testInfo) => {
        // Given
        const rows = Array.from({ length: 51 }, (_, index) => notice(`Tied ${index}`, 'autonomy_l2'))
        const fixture = await prepareNotifications(page, rows)
        await page.clock.install({ time: new Date('2030-01-02T12:00:00Z') })
        await page.route('**/api/notifications/page?**&cursor=Tied+49', (route) => route.fulfill({ json: { items: [rows[49], rows[50]], next_cursor: null } }))
        await page.goto(inboxPath)
        const list = page.getByRole('list', { name: 'Notifications' })
        await expect(list.getByRole('listitem')).toHaveCount(50)
        const dimensions = await page.evaluate(() => ({ width: innerWidth, scale: devicePixelRatio, touch: navigator.maxTouchPoints, dark: document.documentElement.classList.contains('dark') }))
        expect(dimensions.width).toBe(width)
        expect(dimensions.dark).toBe(theme === 'dark')
        if (width === 390) { expect(dimensions.scale).toBe(3); expect(dimensions.touch).toBeGreaterThan(0) }
        await page.screenshot({ path: testInfo.outputPath('first-page.png'), fullPage: true })
        // When
        await page.getByRole('button', { name: 'Load more', exact: true }).click()
        // Then
        await expect(list.getByRole('listitem')).toHaveCount(51)
        await expect(list.getByText('Tied 49', { exact: true })).toHaveCount(1)
        await expect(page.getByRole('button', { name: 'Load more', exact: true })).toHaveCount(0)
        await page.screenshot({ path: testInfo.outputPath('final-page.png'), fullPage: true })
        fixture.insert(notice('Live arrival', 'autonomy_l2'))
        await page.getByRole('button', { name: 'Mark as read: Tied 0', exact: true }).click()
        await expect(list.getByText('Live arrival', { exact: true })).toBeVisible()
        await expect(list.getByRole('listitem')).toHaveCount(50)
        await expect(list.getByText('Tied 50', { exact: true })).toHaveCount(0)
        expect(fixture.requests.at(-1)?.cursor).toBeNull()
        await page.screenshot({ path: testInfo.outputPath('mutation-reset.png'), fullPage: true })
        await page.locator('main').evaluate((element) => { element.scrollTop = element.scrollHeight })
        await page.screenshot({ path: testInfo.outputPath('scrolled-end.png') })
        const scrollers = await page.evaluate(() => [...document.querySelectorAll('*')].filter((element) => /auto|scroll/.test(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight).map((element) => element.tagName.toLowerCase()))
        const actionLog = testInfo.outputPath('cursor-actions.json')
        await writeFile(actionLog, JSON.stringify({ dimensions, scrollers, requests: fixture.requests }, null, 2))
        await testInfo.attach('cursor-actions', { path: actionLog, contentType: 'application/json' })
      })
    })
  }
}

test('an empty final cursor page keeps loaded rows and removes load more', async ({ page }) => {
  // Given
  await prepareNotifications(page, Array.from({ length: 51 }, (_, index) => notice(`Row ${index}`, 'autonomy_l2')))
  await page.route('**/api/notifications/page?**&cursor=Row+49', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.goto(inboxPath)
  const list = page.getByRole('list', { name: 'Notifications' })
  await expect(list.getByRole('listitem')).toHaveCount(50)
  // When
  await page.getByRole('button', { name: 'Load more', exact: true }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Load more', exact: true })).toHaveCount(0)
  await expect(list.getByRole('listitem')).toHaveCount(50)
})

test('a malformed cursor envelope renders a retryable error instead of rows', async ({ page }, testInfo) => {
  // Given
  await prepareNotifications(page, [notice('Valid row', 'broadcast')])
  await page.route('**/api/notifications/page?**', (route) => route.fulfill({ json: { items: [notice('Untrusted row', 'broadcast')], next_cursor: 17 } }))
  // When
  await page.goto(inboxPath)
  // Then
  await expect(page.getByRole('alert')).toContainText('Unable to load notifications')
  await expect(page.getByText('Untrusted row', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('malformed-envelope.png'), fullPage: true })
  await page.unroute('**/api/notifications/page?**')
  await page.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(page.getByText('Valid row', { exact: true })).toBeVisible()
})

test('a mutation discards an outstanding cursor response before restarting the first page', async ({ page }) => {
  // Given
  await prepareNotifications(page, Array.from({ length: 51 }, (_, index) => notice(`Row ${index}`, 'autonomy_l2')))
  const started = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const finished = Promise.withResolvers<void>()
  await page.route('**/api/notifications/page?**&cursor=Row+49', async (route) => {
    started.resolve()
    await release.promise
    await route.fulfill({ json: { items: [notice('Obsolete continuation', 'autonomy_l2')], next_cursor: null } })
    finished.resolve()
  })
  try {
    await page.goto(inboxPath)
    await page.getByRole('button', { name: 'Load more', exact: true }).click()
    await started.promise
    // When
    await page.getByRole('button', { name: 'Archive notification: Row 0', exact: true }).click()
    await expect(page.getByText('Row 50', { exact: true })).toBeVisible()
    release.resolve()
    await finished.promise
    // Then
    await expect(page.getByText('Obsolete continuation', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')).toHaveCount(50)
    await expect(page.getByRole('button', { name: 'Load more', exact: true })).toHaveCount(0)
  } finally { release.resolve() }
})
