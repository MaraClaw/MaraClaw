import { expect, test } from '@playwright/test'

import { prepareSchedules, savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'
import { originalAgent, originalToken, replacementToken, replacementUser, tokenKey, writeFromOtherTab } from './session-fencing-fixture'

for (const status of [200, 401, 503]) {
  test(`late schedule save ${status} cannot reset replacement session alerts or cutoff`, async ({ page }) => {
    await prepareSchedules(page, [savedSchedule])
    await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: {
      ...originalAgent, creator_id: route.request().headers().authorization === `Bearer ${replacementToken}`
        ? replacementUser.id : originalAgent.creator_id,
    } }))
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route(`**${schedulesApiPath}${savedSchedule.id}`, async (route) => {
      if (route.request().method() !== 'PATCH') return route.fallback()
      expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
      expect(route.request().postDataJSON()).toEqual({ timezone: 'Etc/GMT+5', notify_on_failure: true, ends_at: '2030-01-16T01:30:00Z' })
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200
        ? { ...savedSchedule, timezone: 'Etc/GMT+5', notify_on_failure: true, ends_at: '2030-01-16T01:30:00Z', next_run_at: '2026-01-15T14:00:00Z' }
        : { detail: 'Obsolete timezone save refused' } })
    })
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await page.getByLabel('Timezone').selectOption('Etc/GMT+5')
    const alerts = page.getByRole('checkbox', { name: 'Notify me when a scheduled run fails' })
    await alerts.check()
    await page.getByLabel('End time (UTC)', { exact: true }).fill('2030-01-16T01:30')
    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
    await requested.promise
    try {
      await expect(alerts).toBeDisabled()
      await expect(page.getByLabel('End time (UTC)', { exact: true })).toBeDisabled()
      await writeFromOtherTab(page, replacementToken)
      await expect(page.getByLabel('Name', { exact: true })).toHaveValue('')
      await expect(alerts).not.toBeChecked()
      await expect(page.getByLabel('End time (UTC)', { exact: true })).toHaveValue('')
      await page.getByLabel('Name', { exact: true }).fill('Replacement unsaved schedule')
      await page.getByLabel('Instruction', { exact: true }).fill('Replacement unsaved instruction')
      await alerts.check()
      await page.getByLabel('End time (UTC)', { exact: true }).fill('2031-02-03T04:05')
      await page.clock.install()
      const completed = page.waitForEvent('requestfinished', {
        predicate: (request) => request.method() === 'PATCH' && new URL(request.url()).pathname.endsWith(`/${savedSchedule.id}`),
      })

      release.resolve()

      await completed
      await page.clock.runFor(1000)
      await expect(page).toHaveURL(schedulesPath)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
      await expect(alerts).toBeChecked()
      await expect(page.getByLabel('End time (UTC)', { exact: true })).toHaveValue('2031-02-03T04:05')
      await expect(page.getByRole('listitem', { name: savedSchedule.name, exact: true })).toHaveCount(0)
      await expect(page.getByLabel('Name', { exact: true })).toHaveValue('Replacement unsaved schedule')
      await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue('Replacement unsaved instruction')
      await expect(page.getByRole('button', { name: 'Add schedule', exact: true })).toBeEnabled()
      await expect(page.getByRole('status').filter({ hasText: /save/i })).toHaveCount(0)
    } finally { release.resolve() }
  })
}
