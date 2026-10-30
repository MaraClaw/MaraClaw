import { expect, test } from '@playwright/test'

import { activity, activityApiPath, activityPath, prepareActivity } from './activity-fixture'

for (const width of [390, 1440, 1920]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme}`, () => {
      test.use({ viewport: { width, height: 900 }, isMobile: width === 390, hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme, reducedMotion: 'reduce' })
      test('manager timeline wraps summaries and retains keyboard filter controls', async ({ page }, testInfo) => {
        // Given
        await prepareActivity(page)
        await page.route(`**${activityApiPath}*`, (route) => route.fulfill({ json: { items: [
          activity,
          { ...activity, id: 'long', action_type: 'future_tool_execution_type', summary: `Long recorded output ${'long-unbroken-value'.repeat(12)}\n작업 기록을 확인합니다.`, created_at: '2026-10-29T00:00:00Z' },
        ], next_cursor: null } }))
        // When
        await page.goto(activityPath)
        // Then
        await expect(page.getByText(activity.summary, { exact: true })).toBeVisible()
        const input = page.getByLabel('Action type (exact match)')
        await input.focus()
        await expect(input).toBeFocused()
        await page.keyboard.press('Tab')
        await expect(page.getByRole('button', { name: 'Apply filter', exact: true })).toBeFocused()
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        expect(await page.getByRole('region', { name: 'Activity', exact: true }).evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true)
        await page.screenshot({ path: testInfo.outputPath('activity.png') })
        await page.getByRole('button', { name: 'Refresh activity', exact: true }).scrollIntoViewIfNeeded()
        await page.screenshot({ path: testInfo.outputPath('activity-end.png') })
      })
    })
  }
}
