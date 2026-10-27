import { expect, test } from '@playwright/test'

import { fillSchedule, prepareSchedules, savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'
import { historyPath, historyRun } from './schedule-history-fixture'
import { registerScheduleRecurrenceTests } from './schedules-recurrence-cases'

test.use({ timezoneId: 'Pacific/Honolulu' })
registerScheduleRecurrenceTests()

test('creation persists an explicit timezone across reload and displays saved-zone time', async ({ page }) => {
  const fixture = await prepareSchedules(page)
  await page.goto(schedulesPath)
  await expect(page.getByLabel('Timezone')).toHaveValue('UTC')
  await fillSchedule(page)

  await page.getByRole('button', { name: 'Add schedule', exact: true }).click()

  await expect.poll(() => fixture.creations).toEqual([{
    name: savedSchedule.name, instruction: savedSchedule.instruction,
    cron_expr: savedSchedule.cron_expr, timezone: 'Etc/GMT-8', is_enabled: true, notify_on_failure: false,
  }])
  await page.reload()
  const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
  await expect(row).toContainText('GMT +8')
  await expect(row).toContainText('15 Jan 2026, 09:00 GMT+8')
  await row.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('Timezone')).toHaveValue('Etc/GMT-8')
})

test('timezone-only editing sends only timezone and reloads the recomputed next run', async ({ page }) => {
  const fixture = await prepareSchedules(page, [savedSchedule])
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('Timezone')).toHaveValue('Asia/Shanghai')

  await page.getByLabel('Timezone').selectOption('Etc/GMT+5')
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click()

  await expect.poll(() => fixture.patches).toEqual([{ timezone: 'Etc/GMT+5' }])
  const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
  await expect(row).toContainText('GMT -5')
  await expect(row).toContainText('15 Jan 2026, 09:00 GMT-5')
  await page.reload()
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('Timezone')).toHaveValue('Etc/GMT+5')
  await expect(page.getByLabel('Repeat', { exact: true })).toHaveValue('daily')
  await expect(page.getByLabel('Time', { exact: true })).toHaveValue('09:00')
})

test('timezone-only editing preserves an empty instruction across reload', async ({ page }) => {
  const fixture = await prepareSchedules(page, [{ ...savedSchedule, instruction: '' }])
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue('')

  await page.getByLabel('Timezone').selectOption('Etc/GMT+5')
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click()

  await expect.poll(() => fixture.patches).toEqual([{ timezone: 'Etc/GMT+5' }])
  const row = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
  await expect(row).toContainText('GMT -5')
  await page.reload()
  await expect(row).toContainText('GMT -5')
  await row.getByRole('button', { name: 'Edit', exact: true }).click()
  await expect(page.getByLabel('Timezone')).toHaveValue('Etc/GMT+5')
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
  await expect(page.getByLabel('Timezone')).toHaveValue('Asia/Shanghai')
})

test('invalid legacy timezone focuses actionable feedback without sending a request', async ({ page }) => {
  const fixture = await prepareSchedules(page, [{ ...savedSchedule, timezone: 'Not/A_Zone' }])
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Edit', exact: true }).click()

  await expect(page.getByLabel('Timezone')).toHaveValue('Not/A_Zone')
  await page.getByRole('button', { name: 'Save schedule', exact: true }).click()

  await expect(page.getByLabel('Timezone')).toHaveAttribute('aria-invalid', 'true')
  await expect(page.getByLabel('Timezone')).toBeFocused()
  await expect(page.locator('#schedule-timezone-error')).toBeVisible()
  await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue(savedSchedule.instruction)
  expect(fixture.patches).toEqual([])
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
    await page.getByLabel('Timezone').selectOption('Etc/GMT+5')
    await page.getByRole('checkbox', { name: 'Notify me when a scheduled run fails' }).check()

    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
    await requested.promise
    await expect(page.getByLabel('Timezone')).toBeDisabled()
    await expect(page.getByRole('checkbox', { name: 'Notify me when a scheduled run fails' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Saving...', exact: true })).toBeDisabled()
    release.resolve()

    await expect(page.getByRole('button', { name: 'Save schedule', exact: true })).toBeEnabled()
    await expect(page.getByLabel('Timezone')).toHaveValue('Etc/GMT+5')
    await expect(page.getByRole('checkbox', { name: 'Notify me when a scheduled run fails' })).toBeChecked()
    await expect(page.getByRole('status').filter({ hasText: /timezone/i })).toBeVisible()
    await expect(page.getByLabel('Instruction', { exact: true })).toHaveValue(savedSchedule.instruction)
  })
}

test('manage access without creator ownership keeps schedules read-only', async ({ page }) => {
  await prepareSchedules(page, [savedSchedule], false)
  await page.goto(schedulesPath)

  await expect(page.getByRole('listitem', { name: savedSchedule.name, exact: true })).toContainText('Asia/Shanghai')

  await expect(page.getByLabel('Timezone')).toHaveCount(0)
  await expect(page.getByRole('checkbox', { name: 'Notify me when a scheduled run fails' })).toHaveCount(0)
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

test('history distinguishes outcomes and loads older runs without losing rows on a page error', async ({ page }) => {
  await prepareSchedules(page, [savedSchedule])
  const firstRequested = Promise.withResolvers<void>()
  const firstRelease = Promise.withResolvers<void>()
  const olderRequested = Promise.withResolvers<void>()
  const olderRelease = Promise.withResolvers<void>()
  const cursors: (string | null)[] = []
  const cursor = 'opaque/+?=& cursor'
  let olderAttempts = 0
  await page.route(`**${historyPath}*`, async (route) => {
    const url = new URL(route.request().url())
    expect(url.searchParams.get('limit')).toBe('20')
    cursors.push(url.searchParams.get('cursor'))
    if (!url.searchParams.has('cursor')) {
      firstRequested.resolve()
      await firstRelease.promise
      return route.fulfill({ json: { items: [historyRun('latest', 'success'), historyRun('failed', 'failure')], next_cursor: cursor } })
    }
    expect(url.searchParams.get('cursor')).toBe(cursor)
    if (++olderAttempts === 1) return route.fulfill({ status: 503, json: { detail: 'History unavailable' } })
    olderRequested.resolve()
    await olderRelease.promise
    await route.fulfill({ json: { items: [historyRun('cancelled', 'cancelled'), { id: 'legacy', reply: 'Completed successfully' }], next_cursor: null } })
  })
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  await firstRequested.promise
  const history = page.getByRole('region', { name: 'Run history', exact: true })
  await expect(history.getByRole('status')).toHaveText('Loading run history...')
  await expect(history.getByText('No history yet.', { exact: true })).toHaveCount(0)
  firstRelease.resolve()
  await expect(history.getByRole('list', { name: 'Schedule runs' }).getByText('Success', { exact: true })).toBeVisible()
  await expect(history.getByRole('list', { name: 'Schedule runs' }).getByText('Failure', { exact: true })).toBeVisible()

  await history.getByRole('button', { name: 'Load older runs', exact: true }).click()

  await expect(history.getByRole('status')).toHaveText('Unable to load older runs. Try again.')
  await expect(history.getByRole('listitem')).toHaveCount(2)
  await history.getByRole('button', { name: 'Retry older runs', exact: true }).click()
  await olderRequested.promise
  await expect(history.getByRole('button', { name: 'Loading older runs...', exact: true })).toBeDisabled()
  await expect(history.getByRole('status')).toHaveText('Loading older runs...')
  await expect(history.getByRole('listitem')).toHaveCount(2)
  olderRelease.resolve()
  await expect(history.getByRole('listitem')).toHaveCount(4)
  await expect(history.getByRole('list', { name: 'Schedule runs' }).getByText('Cancelled', { exact: true })).toBeVisible()
  await expect(history.getByRole('list', { name: 'Schedule runs' }).getByText('Unknown', { exact: true })).toBeVisible()
  await expect(history.getByText('No older runs.', { exact: true })).toBeVisible()
  await expect(history.getByRole('button')).toHaveCount(0)
  expect(cursors).toEqual([null, cursor, cursor])
})

test('history first-page errors offer a retry and only a successful empty page shows empty state', async ({ page }) => {
  await prepareSchedules(page, [savedSchedule])
  let attempts = 0
  await page.route(`**${historyPath}*`, (route) => ++attempts === 1
    ? route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
    : route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  const history = page.getByRole('region', { name: 'Run history', exact: true })
  await expect(history.getByRole('status')).toHaveText('Unable to load run history. Try again.')
  await expect(history.getByText('No history yet.', { exact: true })).toHaveCount(0)

  await history.getByRole('button', { name: 'Retry history', exact: true }).click()

  await expect(history.getByText('No history yet.', { exact: true })).toBeVisible()
  await expect(history.getByRole('status')).toHaveCount(0)
  await expect(history.getByRole('button')).toHaveCount(0)
  expect(attempts).toBe(2)
})

test('deleting a schedule preserves its open run history and older-page control', async ({ page }) => {
  await prepareSchedules(page, [savedSchedule])
  await page.route(`**${historyPath}*`, (route) => route.fulfill({ json: {
    items: [historyRun(new URL(route.request().url()).searchParams.has('cursor') ? 'older-deleted-run' : 'latest-deleted-run')],
    next_cursor: new URL(route.request().url()).searchParams.has('cursor') ? null : 'older',
  } }))
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  await expect(page.getByText('latest-deleted-run', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Delete', exact: true }).click()

  await expect(page.getByRole('listitem', { name: savedSchedule.name, exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Load older runs' }).click()
  await expect(page.getByText('older-deleted-run', { exact: true })).toBeVisible()
})
