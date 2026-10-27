import { expect, test } from '@playwright/test'

import { fillSchedule, prepareSchedules, savedSchedule, schedulesPath } from './schedules-fixture'

const alertName = 'Notify me when a scheduled run fails'

for (const notify_on_failure of [false, true]) {
  test(`creation persists ${notify_on_failure} failure alerts and resets the next draft to off`, async ({ page }) => {
    // Given
    const fixture = await prepareSchedules(page)
    await page.goto(schedulesPath)
    const alerts = page.getByRole('checkbox', { name: alertName })
    await expect(alerts).not.toBeChecked()
    await fillSchedule(page)
    if (notify_on_failure) {
      await alerts.focus()
      await alerts.press('Space')
    }
    // When
    await page.getByRole('button', { name: 'Add schedule', exact: true }).click()
    // Then
    await expect.poll(() => fixture.creations).toEqual([{
      name: savedSchedule.name, instruction: savedSchedule.instruction, cron_expr: savedSchedule.cron_expr,
      timezone: 'Etc/GMT-8', is_enabled: true, notify_on_failure,
    }])
    await expect(alerts).not.toBeChecked()
    await page.reload()
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await expect(alerts).toBeChecked({ checked: notify_on_failure })
  })

  test(`editing toggles failure alerts to ${notify_on_failure} without resending other fields`, async ({ page }) => {
    // Given
    const fixture = await prepareSchedules(page, [{ ...savedSchedule, notify_on_failure: !notify_on_failure }])
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    const alerts = page.getByRole('checkbox', { name: alertName })
    await expect(alerts).toBeChecked({ checked: !notify_on_failure })
    await alerts.setChecked(notify_on_failure)
    // When
    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
    // Then
    await expect.poll(() => fixture.patches).toEqual([{ notify_on_failure }])
    await expect(alerts).not.toBeChecked()
    await page.reload()
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await expect(alerts).toBeChecked({ checked: notify_on_failure })
  })

  test(`same-session list refresh preserves unsaved ${notify_on_failure} alerts and editor fields`, async ({ page }) => {
    // Given
    const removable = { ...savedSchedule, id: 'removable', name: 'Removable schedule' }
    const fixture = await prepareSchedules(page, [{ ...savedSchedule, notify_on_failure: !notify_on_failure }, removable])
    await page.goto(schedulesPath)
    await page.getByRole('listitem', { name: savedSchedule.name, exact: true }).getByRole('button', { name: 'Edit', exact: true }).click()
    await page.getByLabel('Name', { exact: true }).fill('Unsaved name')
    await page.getByLabel('Instruction', { exact: true }).fill('Unsaved instruction')
    await page.getByLabel('Timezone', { exact: true }).selectOption('Etc/GMT+5')
    const alerts = page.getByRole('checkbox', { name: alertName })
    await alerts.setChecked(notify_on_failure)
    const otherRow = page.getByRole('listitem', { name: removable.name, exact: true })
    // When
    await otherRow.getByRole('button', { name: 'Delete', exact: true }).click()
    // Then
    await expect(otherRow).toHaveCount(0)
    await expect(alerts).toBeChecked({ checked: notify_on_failure })
    await expect(page.getByLabel('Name', { exact: true })).toHaveValue('Unsaved name')
    await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue('Unsaved instruction')
    await expect(page.getByLabel('Timezone', { exact: true })).toHaveValue('Etc/GMT+5')
    expect(fixture.patches).toEqual([])
  })
}

test('canceling enabled failure alerts resets to off without saving the edit', async ({ page }) => {
  // Given
  const fixture = await prepareSchedules(page, [{ ...savedSchedule, notify_on_failure: true }])
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  const alerts = page.getByRole('checkbox', { name: alertName })
  await expect(alerts).toBeChecked()
  await page.getByLabel('Name', { exact: true }).fill('Discarded name')
  // When
  await page.getByRole('button', { name: 'Cancel edit', exact: true }).click()
  // Then
  await expect(alerts).not.toBeChecked()
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue('')
  expect(fixture.patches).toEqual([])
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(alerts).toBeChecked()
})
