import { expect, test } from '@playwright/test'

import { fillSchedule, prepareSchedules, savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'

export function registerScheduleRecurrenceTests() {
  for (const preset of [
    { kind: 'daily', time: '00:00', cron: '0 0 * * *', summary: 'Daily at 00:00' },
    { kind: 'weekdays', time: '23:59', cron: '59 23 * * 1-5', summary: 'Weekdays (Mon–Fri) at 23:59' },
    { kind: 'weekly', time: '16:45', cron: '45 16 * * 0', summary: 'Every Sunday at 16:45' },
    { kind: 'monthly', time: '08:05', cron: '5 8 28 * *', summary: 'Monthly on day 28 at 08:05' },
  ]) {
    test(`${preset.kind} controls persist exact payloads and readable summaries`, async ({ page }) => {
      const fixture = await prepareSchedules(page)
      await page.goto(schedulesPath)
      await fillSchedule(page)
      await page.getByLabel('Repeat', { exact: true }).selectOption(preset.kind)
      await page.getByLabel('Time', { exact: true }).fill(preset.time)
      if (preset.kind === 'weekly') await page.getByLabel('Day of week', { exact: true }).selectOption('0')
      if (preset.kind === 'monthly') {
        await expect(page.getByLabel('Day of month', { exact: true }).getByRole('option')).toHaveCount(28)
        await page.getByLabel('Day of month', { exact: true }).selectOption('28')
      }
      await expect(page.getByLabel('Cron', { exact: true })).toHaveCount(0)
      await expect(page.locator('#schedule-recurrence-summary')).toHaveText(`${preset.summary} · GMT +8`)

      await page.getByRole('button', { name: 'Add schedule', exact: true }).click()

      await expect.poll(() => fixture.creations).toEqual([{
        name: savedSchedule.name, instruction: savedSchedule.instruction,
        cron_expr: preset.cron, timezone: 'Etc/GMT-8', is_enabled: true, notify_on_failure: false,
      }])
      const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
      await expect(row).toContainText(`${preset.summary} · GMT +8`)
      await expect(row).not.toContainText(preset.cron)
      await expect(page.getByLabel('Repeat', { exact: true })).toHaveValue('weekdays')
      await expect(page.getByLabel('Time', { exact: true })).toHaveValue('09:00')
      await expect(page.getByLabel('Timezone')).toHaveValue('UTC')
      await page.reload()
      await row.getByRole('button', { name: 'Edit', exact: true }).click()
      await expect(page.getByLabel('Repeat', { exact: true })).toHaveValue(preset.kind)
      await expect(page.getByLabel('Time', { exact: true })).toHaveValue(preset.time)
    })
  }

  test('recurrence-only editing sends only cron', async ({ page }) => {
    const fixture = await prepareSchedules(page, [savedSchedule])
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()

    await page.getByLabel('Time', { exact: true }).fill('10:30')
    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()

    await expect.poll(() => fixture.patches).toEqual([{ cron_expr: '30 10 * * *' }])
  })

  for (const cron of ['00  09 * * 7 ', '0 9 31 * *', '0 9 * * MON-FRI']) {
    test(`custom ${JSON.stringify(cron)} survives a name-only patch exactly`, async ({ page }) => {
      const fixture = await prepareSchedules(page, [{ ...savedSchedule, cron_expr: cron }])
      await page.goto(schedulesPath)
      await expect(page.getByRole('listitem', { name: savedSchedule.name, exact: true })).toContainText('Custom cron:')
      await page.getByRole('button', { name: 'Edit', exact: true }).click()
      await expect(page.getByLabel('Repeat', { exact: true })).toHaveValue('custom')
      await expect(page.getByLabel('Cron', { exact: true })).toHaveValue(cron)
      await expect(page.getByLabel('Time', { exact: true })).toHaveCount(0)

      await page.getByLabel('Name', { exact: true }).fill('Renamed custom')
      await page.getByRole('button', { name: 'Save schedule', exact: true }).click()

      await expect.poll(() => fixture.patches).toEqual([{ name: 'Renamed custom' }])
      await page.reload()
      await page.getByRole('button', { name: 'Edit', exact: true }).click()
      await expect(page.getByLabel('Cron', { exact: true })).toHaveValue(cron)
    })
  }

  test('Custom mode retains canonical text while typing and intentionally replaces it on preset selection', async ({ page }) => {
    const fixture = await prepareSchedules(page)
    await page.goto(schedulesPath)
    await fillSchedule(page)

    await page.getByLabel('Repeat', { exact: true }).selectOption('custom')
    await expect(page.getByLabel('Cron', { exact: true })).toHaveValue('0 9 * * *')
    await page.getByLabel('Cron', { exact: true }).fill('15 17 * * 2')
    await expect(page.getByLabel('Repeat', { exact: true })).toHaveValue('custom')
    await expect(page.getByLabel('Cron', { exact: true })).toHaveValue('15 17 * * 2')
    await expect(page.locator('#schedule-cron-help')).toContainText('Switching to a simple repeat option replaces it.')
    await page.getByLabel('Repeat', { exact: true }).selectOption('weekdays')
    await expect(page.getByLabel('Time', { exact: true })).toHaveValue('09:00')
    await page.getByLabel('Repeat', { exact: true }).selectOption('custom')
    await expect(page.getByLabel('Cron', { exact: true })).toHaveValue('0 9 * * 1-5')
    await page.getByRole('button', { name: 'Add schedule', exact: true }).click()

    await expect.poll(() => fixture.creations).toEqual([{
      name: savedSchedule.name, instruction: savedSchedule.instruction,
      cron_expr: '0 9 * * 1-5', timezone: 'Etc/GMT-8', is_enabled: true, notify_on_failure: false,
    }])
  })

  test('cleared time remains empty and blocks saving with focused inline feedback', async ({ page }) => {
    const fixture = await prepareSchedules(page, [savedSchedule])
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await page.getByLabel('Time', { exact: true }).fill('')
    await expect(page.getByLabel('Time', { exact: true })).toHaveValue('')

    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()

    await expect(page.getByLabel('Time', { exact: true })).toHaveValue('')
    await expect(page.getByLabel('Time', { exact: true })).toBeFocused()
    await expect(page.getByLabel('Time', { exact: true })).toHaveAttribute('aria-invalid', 'true')
    await expect(page.locator('#schedule-time-error')).toHaveText('Choose a time before saving.')
    expect(fixture.patches).toEqual([])
    await expect(page.getByLabel('Name', { exact: true })).toHaveValue(savedSchedule.name)
  })

  test('canceling Custom edits resets recurrence defaults without a patch', async ({ page }) => {
    const fixture = await prepareSchedules(page, [{ ...savedSchedule, cron_expr: '0 9 31 * *' }])
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await page.getByLabel('Cron', { exact: true }).fill('arbitrary expression')

    await page.getByRole('button', { name: 'Cancel edit', exact: true }).click()

    await expect(page.getByLabel('Repeat', { exact: true })).toHaveValue('weekdays')
    await expect(page.getByLabel('Time', { exact: true })).toHaveValue('09:00')
    await expect(page.getByLabel('Timezone')).toHaveValue('UTC')
    expect(fixture.patches).toEqual([])
  })

  test('custom cron server errors retain exact input and Custom mode', async ({ page }) => {
    await prepareSchedules(page)
    await page.route(`**${schedulesApiPath}`, (route) => route.request().method() === 'POST'
      ? route.fulfill({ status: 422, json: { detail: 'Cron expression is invalid.' } }) : route.fallback())
    await page.goto(schedulesPath)
    await fillSchedule(page)
    await page.getByLabel('Repeat', { exact: true }).selectOption('custom')
    await page.getByLabel('Cron', { exact: true }).fill('not cron  ')

    await page.getByRole('button', { name: 'Add schedule', exact: true }).click()

    await expect(page.getByRole('status').filter({ hasText: 'Cron expression is invalid.' })).toBeVisible()
    await expect(page.getByLabel('Repeat', { exact: true })).toHaveValue('custom')
    await expect(page.getByLabel('Cron', { exact: true })).toHaveValue('not cron  ')
    await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue(savedSchedule.instruction)
  })
}
