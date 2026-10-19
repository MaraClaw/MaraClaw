import { expect, test } from '@playwright/test'

import { fillSchedule, prepareSchedules, savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'

test.use({ timezoneId: 'Pacific/Honolulu' })

test('creation persists an explicit timezone across reload and displays saved-zone time', async ({ page }) => {
  const fixture = await prepareSchedules(page)
  await page.goto(schedulesPath)
  await expect(page.getByLabel('IANA timezone')).toHaveValue('UTC')
  await fillSchedule(page)

  await page.getByRole('button', { name: 'Add schedule', exact: true }).click()

  await expect.poll(() => fixture.creations).toEqual([{
    name: savedSchedule.name, instruction: savedSchedule.instruction,
    cron_expr: savedSchedule.cron_expr, timezone: 'Asia/Shanghai', is_enabled: true,
  }])
  await page.reload()
  const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
  await expect(row).toContainText('Asia/Shanghai')
  await expect(row).toContainText('15 Jan 2026, 09:00 GMT+8')
  await row.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('IANA timezone')).toHaveValue('Asia/Shanghai')
})

test('timezone-only editing sends only timezone and reloads the recomputed next run', async ({ page }) => {
  const fixture = await prepareSchedules(page, [savedSchedule])
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('IANA timezone')).toHaveValue('Asia/Shanghai')

  await page.getByLabel('IANA timezone').fill('America/New_York')
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click()

  await expect.poll(() => fixture.patches).toEqual([{ timezone: 'America/New_York' }])
  const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
  await expect(row).toContainText('America/New_York')
  await expect(row).toContainText('15 Jan 2026, 09:00 GMT-5')
  await page.reload()
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('IANA timezone')).toHaveValue('America/New_York')
  await expect(page.getByLabel('Cron', { exact: true })).toHaveValue(savedSchedule.cron_expr)
})

test('timezone-only editing preserves an empty instruction across reload', async ({ page }) => {
  const fixture = await prepareSchedules(page, [{ ...savedSchedule, instruction: '' }])
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue('')

  await page.getByLabel('IANA timezone').fill('America/New_York')
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click()

  await expect.poll(() => fixture.patches).toEqual([{ timezone: 'America/New_York' }])
  const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
  await expect(row).toContainText('America/New_York')
  await page.reload()
  await expect(row).toContainText('America/New_York')
  await row.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('IANA timezone')).toHaveValue('America/New_York')
  await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue('')
})

test('editing another field preserves the timezone through patch omission', async ({ page }) => {
  const fixture = await prepareSchedules(page, [savedSchedule])
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Edit', exact: true }).click()

  await page.getByLabel('Name', { exact: true }).fill('Renamed report')
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click()

  await expect.poll(() => fixture.patches).toEqual([{ name: 'Renamed report' }])
  await page.reload()
  const row = page.getByRole('listitem', { name: 'Renamed report', exact: true })
  await expect(row).toContainText('Asia/Shanghai')
  await row.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('IANA timezone')).toHaveValue('Asia/Shanghai')
})

test('invalid timezone focuses actionable feedback without sending a request', async ({ page }) => {
  const fixture = await prepareSchedules(page)
  await page.goto(schedulesPath)
  await fillSchedule(page)

  await page.getByLabel('IANA timezone').fill('Not/A_Zone')
  await page.getByRole('button', { name: 'Add schedule', exact: true }).click()

  await expect(page.getByLabel('IANA timezone')).toHaveAttribute('aria-invalid', 'true')
  await expect(page.getByLabel('IANA timezone')).toBeFocused()
  await expect(page.locator('#schedule-timezone-error')).toBeVisible()
  await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue(savedSchedule.instruction)
  expect(fixture.creations).toEqual([])
})

for (const status of [422, 503]) {
  test(`save error ${status} preserves the editor and releases pending controls`, async ({ page }) => {
    await prepareSchedules(page, [savedSchedule])
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route(`**${schedulesApiPath}${savedSchedule.id}`, async (route) => {
      if (route.request().method() !== 'PATCH') return route.fallback()
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: { detail: 'Timezone data unavailable. Ask your administrator to update timezone data.' } })
    })
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await page.getByLabel('IANA timezone').fill('America/New_York')

    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
    await requested.promise
    await expect(page.getByLabel('IANA timezone')).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Saving...', exact: true })).toBeDisabled()
    release.resolve()

    await expect(page.getByRole('button', { name: 'Save schedule', exact: true })).toBeEnabled()
    await expect(page.getByLabel('IANA timezone')).toHaveValue('America/New_York')
    await expect(page.getByRole('status').filter({ hasText: /timezone/i })).toBeVisible()
    await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue(savedSchedule.instruction)
  })
}

test('manage access without creator ownership keeps schedules read-only', async ({ page }) => {
  await prepareSchedules(page, [savedSchedule], false)
  await page.goto(schedulesPath)

  await expect(page.getByRole('listitem', { name: savedSchedule.name, exact: true })).toContainText('Asia/Shanghai')

  await expect(page.getByLabel('IANA timezone')).toHaveCount(0)
  for (const name of ['Add schedule', 'Run', 'Edit', 'History', 'Delete']) {
    await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0)
  }
})

test('unavailable saved timezone is named without falling back to browser-local time', async ({ page }) => {
  await prepareSchedules(page, [{ ...savedSchedule, timezone: 'Not/A_Zone' }])
  await page.goto(schedulesPath)

  const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })

  await expect(row).toContainText('Not/A_Zone')
  await expect(row).toContainText('Timezone unavailable')
  await expect(row).not.toContainText('15 Jan 2026')
})
