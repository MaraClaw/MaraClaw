import { expect, test } from '@playwright/test'

import { admissionPage, prepareAdmission } from './admission-withdraw-fixture'
import { historyPath, historyRun } from './schedule-history-fixture'
import { prepareSchedules, savedSchedule, schedulesPath } from './schedules-fixture'

for (const width of [390, 1440, 1920]) {
  for (const theme of ['light', 'dark'] as const) {
    test.describe(`${width}px ${theme} proposal surfaces`, () => {
      test.use({
        viewport: { width, height: width === 390 ? 844 : 1000 },
        isMobile: width === 390, hasTouch: width === 390,
        deviceScaleFactor: width === 390 ? 3 : 1,
        colorScheme: theme, reducedMotion: 'reduce',
      })

      test('schedule filter and accepted Run remain legible and keyboard operable', async ({ page }, testInfo) => {
        // Given
        await prepareSchedules(page, [savedSchedule])
        await page.addInitScript((value) => localStorage.setItem('maraclaw-theme', value), theme)
        await page.route(`**${historyPath}*`, (route) => route.fulfill({ json: {
          items: [historyRun('Daily report delivered', 'success'), historyRun('A previous run was cancelled', 'cancelled')], next_cursor: null,
        } }))
        await page.goto(schedulesPath)
        await page.getByRole('button', { name: 'History', exact: true }).click()
        const history = page.getByRole('region', { name: 'Run history' })
        await expect(history.getByRole('listitem')).toHaveCount(2)
        // When
        await history.getByLabel('Outcome').focus()
        // Then
        await expect(history.getByLabel('Outcome')).toBeFocused()
        await expect(history).toBeInViewport()
        await page.screenshot({ path: testInfo.outputPath('history-focused.png') })
        await history.getByRole('listitem').last().scrollIntoViewIfNeeded()
        await page.screenshot({ path: testInfo.outputPath('history-end.png') })
        await page.getByRole('button', { name: 'Run', exact: true }).click()
        await expect(page.getByText('Run accepted. Check history for completion.')).toBeVisible()
        await page.screenshot({ path: testInfo.outputPath('run-accepted.png') })
      })

      test('pending, withdrawn and list admission states preserve responsive layout', async ({ page }, testInfo) => {
        // Given
        await prepareAdmission(page)
        await page.addInitScript((value) => localStorage.setItem('maraclaw-theme', value), theme)
        await page.goto(admissionPage)
        const button = page.getByRole('button', { name: 'Withdraw request', exact: true })
        await button.focus()
        await expect(button).toBeFocused()
        await page.screenshot({ path: testInfo.outputPath('admission-pending.png') })
        // When
        await button.press('Enter')
        // Then
        await expect(page.getByRole('region', { name: 'Admission status' }).getByText('Withdrawn', { exact: true })).toBeVisible()
        const dimensions = await page.locator('.admission-content').evaluate((element) => ({
          width: element.clientWidth, scrollWidth: element.scrollWidth,
        }))
        expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.width)
        await page.screenshot({ path: testInfo.outputPath('admission-withdrawn.png') })
        await page.getByRole('link', { name: 'All admission requests' }).click()
        await expect(page).toHaveURL('/app/admissions')
        await expect(page.getByRole('listitem').getByText('Withdrawn', { exact: true })).toBeVisible()
        await page.screenshot({ path: testInfo.outputPath('admissions-list.png') })
      })
    })
  }
}
