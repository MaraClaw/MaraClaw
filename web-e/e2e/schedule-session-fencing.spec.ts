import { expect, test } from '@playwright/test'

import { prepareSchedules, savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'
import { originalToken, replacementToken, replacementUser, tokenKey, writeFromOtherTab } from './session-fencing-fixture'

for (const status of [200, 401, 503]) {
  test(`late schedule save ${status} cannot affect a replacement session`, async ({ page }) => {
    await prepareSchedules(page, [savedSchedule])
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route(`**${schedulesApiPath}${savedSchedule.id}`, async (route) => {
      if (route.request().method() !== 'PATCH') return route.fallback()
      expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200
        ? { ...savedSchedule, timezone: 'America/New_York', next_run_at: '2026-01-15T14:00:00Z' }
        : { detail: 'Obsolete timezone save refused' } })
    })
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await page.getByLabel('IANA timezone').fill('America/New_York')
    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
    await requested.promise
    await writeFromOtherTab(page, replacementToken)
    await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
    await expect(page.getByText(`Signed in as ${replacementUser.display_name}.`, { exact: false })).toBeVisible()
    await page.clock.install()
    const completed = page.waitForEvent('requestfinished', {
      predicate: (request) => request.method() === 'PATCH' && new URL(request.url()).pathname.endsWith(`/${savedSchedule.id}`),
    })

    release.resolve()

    await completed
    await page.clock.runFor(1000)
    await expect(page).toHaveURL('/app/settings')
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
    await expect(page.getByLabel('Display name')).toHaveValue(replacementUser.display_name)
  })
}
