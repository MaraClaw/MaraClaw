import { expect, test } from '@playwright/test'

import { historyPath } from './schedule-history-fixture'
import { prepareSchedules, savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'
import { originalToken, replacementToken, tokenKey, writeFromOtherTab } from './session-fencing-fixture'

const runPath = `${schedulesApiPath}${savedSchedule.id}/run`
const accepted = { status: 'triggered', schedule_id: savedSchedule.id, run_id: 'durable-manual-run' } as const

for (const uncertainty of ['transport', 'server', 'malformed'] as const) {
  test(`disabled schedule retries ${uncertainty} uncertainty with the same intent, then starts a fresh intent after acceptance`, async ({ page }) => {
    // Given
    await prepareSchedules(page, [{ ...savedSchedule, is_enabled: false, next_run_at: null }])
    const keys: (string | undefined)[] = []
    await page.route(`**${runPath}`, async (route) => {
      const request = route.request()
      expect(request.method()).toBe('POST')
      expect(request.postData()).toBeNull()
      keys.push(request.headers()['idempotency-key'])
      if (keys.length > 1) return route.fulfill({ json: accepted })
      switch (uncertainty) {
        case 'transport': return route.abort('connectionreset')
        case 'server': return route.fulfill({ status: 503, json: {} })
        case 'malformed': return route.fulfill({ json: { status: 'triggered' } })
        default: uncertainty satisfies never
      }
    })
    await page.route(`**${historyPath}*`, (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
    await page.goto(schedulesPath)
    const row = page.getByRole('listitem', { name: savedSchedule.name })
    const run = row.getByRole('button', { name: 'Run', exact: true })
    await expect(run).toBeEnabled()
    await run.click()
    await expect(row.getByText(/Acceptance could not be confirmed/)).toBeVisible()
    // When: retry the uncertain intent, not a second run.
    await run.click()
    // Then
    await expect(row.getByText('Run accepted. Check history for completion.')).toBeVisible()
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/)
    expect(keys[1]).toBe(keys[0])
    await row.getByRole('button', { name: 'History', exact: true }).click()
    await expect(page.getByRole('region', { name: 'Run history' }).getByRole('listitem')).toHaveCount(0)
    await run.click()
    await expect(row.getByText('Run accepted. Check history for completion.')).toBeVisible()
    expect(keys).toHaveLength(3)
    expect(keys[2]).not.toBe(keys[1])
  })
}

test('a definite refused Run clears its intent before the next attempt', async ({ page }) => {
  // Given
  await prepareSchedules(page, [savedSchedule])
  const keys: (string | undefined)[] = []
  await page.route(`**${runPath}`, async (route) => {
    keys.push(route.request().headers()['idempotency-key'])
    await route.fulfill(keys.length === 1 ? { status: 403, json: { detail: 'Run permission refused' } } : { json: accepted })
  })
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  await expect(page.getByText('Run permission refused', { exact: true })).toBeVisible()
  // When
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  // Then
  await expect(page.getByText('Run accepted. Check history for completion.')).toBeVisible()
  expect(keys).toHaveLength(2)
  expect(keys[0]).not.toBe(keys[1])
})

for (const status of [200, 401, 503]) {
  test(`late Run ${status} cannot update or expire a replacement workspace`, async ({ page }) => {
    // Given
    await prepareSchedules(page, [savedSchedule])
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route(`**${runPath}`, async (route) => {
      expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200 ? accepted : { detail: 'Retired Run owner' } })
    })
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Run', exact: true }).click()
    await requested.promise
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeDisabled()
    await writeFromOtherTab(page, replacementToken)
    await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
    const completed = page.waitForEvent('requestfinished', { predicate: (request) => request.url().endsWith(runPath) })
    // When
    release.resolve()
    await completed
    // Then
    await expect(page).toHaveURL('/app/settings')
    await expect(page.getByText(/Run accepted|Retired Run owner|Acceptance could not be confirmed/)).toHaveCount(0)
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
  })
}
