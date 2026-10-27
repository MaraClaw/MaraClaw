import { expect, test } from '@playwright/test'
import { z } from 'zod'

import { prepareSchedules, savedSchedule, schedulesPath } from './schedules-fixture'
import { originalToken, originalUser, replacementToken, replacementUser, tokenKey, writeFromOtherTab } from './session-fencing-fixture'

test('Settings timezone persists across reload and seeds a new schedule with an explicit override', async ({ page }) => {
  // Given
  const fixture = await prepareSchedules(page)
  let timezone: string | null = null
  const patches: unknown[] = []
  await page.route('**/api/member/auth/me', async (route) => {
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    if (route.request().method() === 'PATCH') {
      const body = z.object({ timezone: z.string() }).strict().parse(route.request().postDataJSON())
      patches.push(body)
      timezone = body.timezone
    } else expect(route.request().method()).toBe('GET')
    await route.fulfill({ json: { ...originalUser, timezone } })
  })
  await page.goto('/app/settings')
  await expect(page.getByRole('combobox', { name: 'Timezone', exact: true })).toHaveValue('UTC')
  await page.getByLabel('Timezone', { exact: true }).selectOption({ label: 'GMT +7' })
  // When
  await page.getByRole('button', { name: 'Save timezone', exact: true }).click()
  // Then
  await expect(page.getByRole('status').filter({ hasText: 'Timezone updated' })).toBeVisible()
  expect(patches).toEqual([{ timezone: 'Etc/GMT-7' }])
  await page.reload()
  await expect(page.getByLabel('Timezone', { exact: true })).toHaveValue('Etc/GMT-7')
  await page.goto(schedulesPath)
  await expect(page.getByLabel('Timezone', { exact: true })).toHaveValue('Etc/GMT-7')
  await expect(page.locator('#schedule-recurrence-summary')).toContainText('GMT +7')
  await page.getByLabel('Name', { exact: true }).fill('Preference override')
  await page.getByLabel('Timezone', { exact: true }).selectOption({ label: 'GMT -5' })
  await page.getByRole('button', { name: 'Add schedule', exact: true }).click()
  await expect.poll(() => fixture.creations).toEqual([{
    name: 'Preference override', cron_expr: '0 9 * * 1-5', instruction: '', timezone: 'Etc/GMT+5', is_enabled: true, notify_on_failure: false,
  }])
  await expect(page.getByLabel('Timezone', { exact: true })).toHaveValue('Etc/GMT-7')
})

for (const timezone of ['Asia/Shanghai', 'America/New_York']) {
  test(`legacy ${timezone} remains the exact selected option and is omitted from name-only edits`, async ({ page }) => {
    // Given
    const fixture = await prepareSchedules(page, [{ ...savedSchedule, timezone }])
    await page.route('**/api/member/auth/me', (route) => route.fulfill({ json: { ...originalUser, timezone: 'Etc/GMT-7' } }))
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await expect(page.getByLabel('Timezone')).toHaveValue(timezone)
    await expect(page.getByLabel('Timezone').getByRole('option', { selected: true })).toHaveText(timezone)
    await page.getByLabel('Name', { exact: true }).fill('Legacy unchanged')
    // When
    await page.getByRole('button', { name: 'Save schedule', exact: true }).click()
    // Then
    await expect.poll(() => fixture.patches).toEqual([{ name: 'Legacy unchanged' }])
    await expect(page.getByLabel('Timezone')).toHaveValue('Etc/GMT-7')
    await page.reload()
    await page.getByRole('button', { name: 'Edit', exact: true }).click()
    await expect(page.getByLabel('Timezone')).toHaveValue(timezone)
    await page.getByRole('button', { name: 'Cancel edit', exact: true }).click()
    await expect(page.getByLabel('Timezone')).toHaveValue('Etc/GMT-7')
  })
}

test('timezone preference pending and error states retain the selected draft and allow retry', async ({ page }) => {
  // Given
  await prepareSchedules(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/member/auth/me', async (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    expect(route.request().postDataJSON()).toEqual({ timezone: 'Etc/GMT-7' })
    requested.resolve()
    await release.promise
    await route.fulfill({ status: 503, json: { detail: 'Timezone preferences unavailable' } })
  })
  await page.goto('/app/settings')
  await page.getByLabel('Timezone').selectOption('Etc/GMT-7')
  // When
  await page.getByRole('button', { name: 'Save timezone', exact: true }).click()
  await requested.promise
  try {
    // Then
    await expect(page.getByLabel('Timezone')).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Saving timezone...', exact: true })).toBeDisabled()
  } finally { release.resolve() }
  // 5xx responses never surface raw server detail; userFacingRequestError always falls back for status >= 500.
  await expect(page.getByRole('status').filter({ hasText: /timezone/i })).toBeVisible()
  await expect(page.getByLabel('Timezone')).toHaveValue('Etc/GMT-7')
  await expect(page.getByRole('button', { name: 'Save timezone', exact: true })).toBeEnabled()
})

for (const status of [200, 401, 503]) {
  test(`late timezone preference ${status} cannot refresh or notify a replacement owner`, async ({ page }) => {
    // Given
    await prepareSchedules(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route('**/api/member/auth/me', async (route) => {
      if (route.request().method() !== 'PATCH') return route.fallback()
      expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200
        ? { ...originalUser, timezone: 'Etc/GMT-7' } : { detail: 'Obsolete timezone preference refused' } })
    })
    await page.goto('/app/settings')
    await page.getByLabel('Timezone').selectOption('Etc/GMT-7')
    await page.getByRole('button', { name: 'Save timezone', exact: true }).click()
    await requested.promise
    try {
      await writeFromOtherTab(page, replacementToken)
      await expect(page.getByLabel('Display name')).toHaveValue(replacementUser.display_name)
      await expect(page.getByLabel('Timezone')).toHaveValue('UTC')
      await page.clock.install()
      const completed = page.waitForEvent('requestfinished', {
        predicate: (request) => request.method() === 'PATCH' && new URL(request.url()).pathname === '/api/member/auth/me',
      })
      // When
      release.resolve()
      await completed
      await page.clock.runFor(1000)
      // Then
      await expect(page.getByLabel('Display name')).toHaveValue(replacementUser.display_name)
      await expect(page.getByLabel('Timezone')).toHaveValue('UTC')
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      await expect(page.getByRole('status').filter({ hasText: /timezone/i })).toHaveCount(0)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
    } finally { release.resolve() }
  })
}
