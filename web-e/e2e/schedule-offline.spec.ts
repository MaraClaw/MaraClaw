import { expect, test } from '@playwright/test'

import { fillSchedule, prepareSchedules, savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'

const alertName = 'Notify me when a scheduled run fails'

test('uncached schedules announce a paused load and resume on reconnect', async ({ page, context }) => {
  // Given
  await prepareSchedules(page, [savedSchedule])
  await page.route('**/api/agent-deletion-requests?*', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.route(`**${schedulesApiPath}`, (route) => route.fulfill({ status: 422, json: { detail: 'Schedule list unavailable' } }))
  await page.goto(schedulesPath)
  await expect(page.getByRole('status').filter({ hasText: /Schedule list unavailable/ })).toBeVisible({ timeout: 15_000 })
  await page.getByRole('navigation', { name: 'Agent sections' }).getByRole('link', { name: 'Settings', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Original company agent', exact: true })).toBeVisible()
  await context.setOffline(true)
  await page.unroute(`**${schedulesApiPath}`)
  // When
  await page.getByRole('link', { name: 'Schedules', exact: true }).click()
  // Then
  await expect(page.getByRole('status').filter({ hasText: /waiting for connection.*schedules/i })).toBeVisible()
  await expect(page.getByText('Loading schedules...', { exact: true })).toHaveCount(0)
  await expect(page.getByText('No schedules yet.', { exact: true })).toHaveCount(0)
  await context.setOffline(false)
  await expect(page.getByRole('listitem', { name: savedSchedule.name, exact: true })).toBeVisible()
  await expect(page.getByRole('status').filter({ hasText: /waiting for connection/i })).toHaveCount(0)
})

for (const editing of [false, true]) {
  test(`paused ${editing ? 'edit' : 'create'} retains its draft until reconnect ${editing ? 'failure' : 'success'}`, async ({ page, context }) => {
    // Given
    const fixture = await prepareSchedules(page, editing ? [savedSchedule] : [])
    if (editing) await page.route(`**${schedulesApiPath}${savedSchedule.id}`, (route) => route.fulfill({
      status: 422, json: { detail: 'Synthetic schedule save unavailable. Please try again.' },
    }))
    await page.goto(schedulesPath)
    await expect(page.getByText(editing ? savedSchedule.name : 'No schedules yet.', { exact: true })).toBeVisible()
    if (editing) await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await fillSchedule(page)
    await page.getByLabel('Name', { exact: true }).fill('Offline draft')
    const alerts = page.getByRole('checkbox', { name: alertName })
    await alerts.check()
    await context.setOffline(true)
    // When
    await page.getByRole('button', { name: editing ? 'Save schedule' : 'Add schedule', exact: true }).click()
    // Then
    const waiting = page.getByRole('status').filter({ hasText: /waiting for connection.*save schedule/i })
    await expect(waiting).toBeVisible()
    expect(await waiting.evaluate((element) => element.closest('[aria-busy="true"]') === null)).toBe(true)
    await expect(page.getByLabel('Name', { exact: true })).toHaveValue('Offline draft')
    await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue(savedSchedule.instruction)
    await expect(page.getByLabel('Timezone', { exact: true })).toHaveValue('Etc/GMT-8')
    await expect(alerts).toBeChecked()
    await expect(alerts).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Saving...', exact: true })).toBeDisabled()
    expect(fixture.creations).toEqual([])
    expect(fixture.patches).toEqual([])
    await context.setOffline(false)
    if (editing) {
      await expect(page.getByRole('status').filter({ hasText: /Synthetic schedule save unavailable/ })).toBeVisible()
      await expect(page.getByLabel('Name', { exact: true })).toHaveValue('Offline draft')
      await expect(alerts).toBeChecked()
      await expect(page.getByRole('button', { name: 'Save schedule', exact: true })).toBeEnabled()
    } else {
      await expect.poll(() => fixture.creations.length).toBe(1)
      expect(fixture.creations[0]).toMatchObject({ name: 'Offline draft', notify_on_failure: true })
      await expect(page.getByLabel('Name', { exact: true })).toHaveValue('')
      await expect(alerts).not.toBeChecked()
    }
    await expect(waiting).toHaveCount(0)
  })
}

for (const width of [390, 1440, 1920]) for (const colorScheme of ['light', 'dark'] as const) {
  test(`notification label is a 44px clickable target at ${width} ${colorScheme}`, async ({ page }) => {
    // Given
    await page.setViewportSize({ width, height: 1000 })
    await page.emulateMedia({ colorScheme })
    await prepareSchedules(page, [savedSchedule])
    await page.goto(schedulesPath)
    const alerts = page.getByRole('checkbox', { name: alertName })
    const label = page.locator('label').filter({ has: alerts })
    // When
    await label.click({ position: { x: 40, y: 2 } })
    // Then
    await expect(alerts).toBeChecked()
    const bounds = await label.boundingBox()
    expect(bounds?.height).toBeGreaterThanOrEqual(44)
    const checkboxBounds = await alerts.boundingBox()
    expect(checkboxBounds?.height).toBe(20)
    expect(checkboxBounds?.width).toBe(20)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await expect(alerts).not.toBeChecked()
    await label.click({ position: { x: 40, y: 2 } })
    await expect(alerts).toBeChecked()
    await label.click({ position: { x: 40, y: 42 } })
    await expect(alerts).not.toBeChecked()
  })
}
