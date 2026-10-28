import { expect, test } from '@playwright/test'

import { fillSchedule, prepareSchedules, savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'

test.use({ timezoneId: 'Pacific/Honolulu' })

const cutoff = '2030-01-16T09:30:45.123456+08:00'
const endLabel = 'End time (UTC)'

test('creation saves the UTC instant rather than browser-local or recurrence time', async ({ page }) => {
  // Given
  const fixture = await prepareSchedules(page)
  await page.goto(schedulesPath)
  await fillSchedule(page)
  await page.getByLabel(endLabel, { exact: true }).fill('2030-01-16T01:30')
  // When
  await page.getByRole('button', { name: 'Add schedule', exact: true }).click()
  // Then
  await expect.poll(() => fixture.creations).toEqual([{
    name: savedSchedule.name, instruction: savedSchedule.instruction, cron_expr: savedSchedule.cron_expr,
    timezone: 'Etc/GMT-8', is_enabled: true, notify_on_failure: false, ends_at: '2030-01-16T01:30:00Z',
  }])
  await page.reload()
  const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
  await expect(row).toContainText('Ends before 16 Jan 2030, 01:30:00 UTC')
  await row.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel(endLabel, { exact: true })).toHaveValue('2030-01-16T01:30')
})

for (const ends_at of [cutoff, '2030-01-16T09:30:00+08:00', null, undefined]) {
  test(`timezone-only edit omits and preserves ${String(ends_at)} cutoff across reload`, async ({ page }) => {
    // Given
    const fixture = await prepareSchedules(page, [{ ...savedSchedule, ends_at }])
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    // When
    await page.getByLabel('Timezone', { exact: true }).selectOption('Etc/GMT+5')
    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
    // Then
    await expect.poll(() => fixture.patches).toEqual([{ timezone: 'Etc/GMT+5' }])
    await page.reload()
    const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
    if (ends_at) {
      await expect(row.locator('time')).toHaveAttribute('datetime', ends_at)
      await expect(row.locator('time')).toHaveText(ends_at === cutoff ? '16 Jan 2030, 01:30:45.123456 UTC' : '16 Jan 2030, 01:30:00 UTC')
    }
    else await expect(row).toContainText('No end time')
  })
}

for (const value of ['2031-02-03T04:05', '']) {
  test(`editing a saved cutoff to ${value || 'blank'} sends only its replacement or explicit null`, async ({ page }) => {
    // Given
    const fixture = await prepareSchedules(page, [{ ...savedSchedule, ends_at: cutoff }])
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await expect(page.getByLabel(endLabel, { exact: true })).toHaveValue('2030-01-16T01:30:45.123')
    // When
    await page.getByLabel(endLabel, { exact: true }).fill(value)
    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
    // Then
    await expect.poll(() => fixture.patches).toEqual([{ ends_at: value ? '2031-02-03T04:05:00Z' : null }])
    await page.reload()
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await expect(page.getByLabel(endLabel, { exact: true })).toHaveValue(value)
  })
}

test('cutoff rejection preserves the UTC draft and enables correction', async ({ page }) => {
  // Given
  await prepareSchedules(page, [{ ...savedSchedule, ends_at: cutoff }])
  await page.route(`**${schedulesApiPath}${savedSchedule.id}`, (route) => route.request().method() === 'PATCH'
    ? route.fulfill({ status: 422, json: { detail: 'End time is invalid. Choose a valid UTC instant.' } }) : route.fallback())
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  await page.getByLabel(endLabel, { exact: true }).fill('2031-02-03T04:05')
  // When
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
  // Then
  await expect(page.getByRole('status').filter({ hasText: 'End time is invalid' })).toBeVisible()
  await expect(page.getByLabel(endLabel, { exact: true })).toHaveValue('2031-02-03T04:05')
  await expect(page.getByRole('button', { name: 'Save schedule', exact: true })).toBeEnabled()
})

test('read-only members see the absolute UTC cutoff without editor controls', async ({ page }) => {
  // Given
  await prepareSchedules(page, [{ ...savedSchedule, ends_at: cutoff }], false)
  // When
  await page.goto(schedulesPath)
  // Then
  await expect(page.getByRole('listitem', { name: savedSchedule.name, exact: true }).locator('time')).toHaveText('16 Jan 2030, 01:30:45.123456 UTC')
  await expect(page.getByLabel(endLabel, { exact: true })).toHaveCount(0)
})
