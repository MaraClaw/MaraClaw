import { expect, test } from '@playwright/test'

import { historyPath } from './schedule-history-fixture'
import { savedSchedule, schedulesPath } from './schedules-fixture'
import { manualAcceptance as accepted, prepareRunSchedules as prepareSchedules, receiptPath, runPath, runReceipt } from './schedule-run-fixture'
import { originalToken, replacementToken, tokenKey, writeFromOtherTab } from './session-fencing-fixture'

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
      if (keys.length > 1) return route.fulfill({ json: { ...accepted, run_id: keys.length === 2 ? accepted.run_id : 'fresh-intent-run' } })
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
    await expect(row.getByRole('region', { name: 'Current run' })).toContainText(accepted.run_id)
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/)
    expect(keys[1]).toBe(keys[0])
    await row.getByRole('button', { name: 'History', exact: true }).click()
    await expect(page.getByRole('region', { name: 'Run history' }).getByRole('listitem')).toHaveCount(0)
    await run.click()
    await expect(row.getByText('Run accepted. Check history for completion.')).toBeVisible()
    await expect(row.getByRole('region', { name: 'Current run' })).toContainText('fresh-intent-run')
    expect(keys).toHaveLength(3)
    expect(keys[2]).not.toBe(keys[1])
  })
}

test('cancelling a pending run fences an in-flight pending poll and stops polling', async ({ page }) => {
  // Given
  await prepareSchedules(page)
  await page.clock.install()
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  let reads = 0
  await page.route(`**${receiptPath}`, async (route) => {
    if (++reads > 1) { requested.resolve(); await release.promise }
    await route.fulfill({ json: runReceipt() })
  })
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  const receipt = page.getByRole('region', { name: 'Current run' })
  await expect(receipt.getByText('Pending', { exact: true })).toBeVisible()
  await page.clock.runFor(2100)
  await requested.promise
  // When
  await receipt.getByRole('button', { name: 'Cancel pending run' }).click()
  await expect(receipt.getByText('Cancelled', { exact: true })).toBeVisible()
  release.resolve()
  await page.clock.runFor(8000)
  // Then
  await expect(receipt.getByText('Cancelled', { exact: true })).toBeVisible()
  await expect(receipt.getByRole('button')).toHaveCount(0)
  expect(reads).toBe(2)
})

for (const [terminal, label] of [['success', 'Success'], ['failure', 'Failure'], ['cancelled', 'Cancelled'], ['unknown', 'Unknown']] as const) {
  test(`an accepted receipt polls pending to running to ${terminal}, then stops`, async ({ page }) => {
    // Given
    await prepareSchedules(page)
    await page.clock.install()
    let status: 'pending' | 'running' | typeof terminal = 'pending'
    let reads = 0
    await page.route(`**${receiptPath}`, (route) => { reads++; return route.fulfill({ json: runReceipt(status) }) })
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Run', exact: true }).click()
    const receipt = page.getByRole('region', { name: 'Current run' })
    await expect(receipt.getByRole('button', { name: 'Cancel pending run' })).toBeVisible()
    status = 'running'
    await page.clock.runFor(2100)
    await expect(receipt.getByText('Running', { exact: true })).toBeVisible()
    await expect(receipt.getByRole('button')).toHaveCount(0)
    // When
    status = terminal
    await page.clock.runFor(2100)
    // Then
    await expect(receipt.getByText(label, { exact: true })).toBeVisible()
    const terminalReads = reads
    await page.clock.runFor(8000)
    expect(reads).toBe(terminalReads)
  })
}

test('cancel 409 refreshes truthful running state without claiming cancellation', async ({ page }) => {
  // Given
  await prepareSchedules(page)
  await page.clock.install()
  let status: 'pending' | 'running' | 'success' = 'pending'
  let reads = 0
  await page.route(`**${receiptPath}`, (route) => { reads++; return route.fulfill({ json: runReceipt(status) }) })
  await page.route(`**${receiptPath}/cancel`, (route) => {
    status = 'running'
    return route.fulfill({ status: 409, json: { detail: 'Run is no longer pending' } })
  })
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  const receipt = page.getByRole('region', { name: 'Current run' })
  // When
  await receipt.getByRole('button', { name: 'Cancel pending run' }).click()
  // Then
  await expect(receipt.getByText('Running', { exact: true })).toBeVisible()
  await expect(receipt.getByRole('status')).toHaveText(/Cancellation lost the race.*did not stop/)
  await expect(receipt.getByText('Cancelled', { exact: true })).toHaveCount(0)
  expect(reads).toBe(2)
  status = 'success'
  await page.clock.runFor(2100)
  await expect(receipt.getByText('Success', { exact: true })).toBeVisible()
})

for (const status of [403, 404]) {
  test(`receipt HTTP ${status} stops polling and removes cancellation controls`, async ({ page }) => {
    // Given
    await prepareSchedules(page)
    await page.clock.install()
    let reads = 0
    await page.route(`**${receiptPath}`, (route) => ++reads === 1 ? route.fulfill({ json: runReceipt() })
      : route.fulfill({ status, json: { detail: 'Receipt unavailable' } }))
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'Run', exact: true }).click()
    const receipt = page.getByRole('region', { name: 'Current run' })
    await expect(receipt.getByText('Pending', { exact: true })).toBeVisible()
    // When
    await page.clock.runFor(2100)
    // Then
    await expect(receipt.getByRole('status')).toHaveText('Receipt unavailable')
    await expect(receipt.getByRole('button')).toHaveCount(0)
    await page.clock.runFor(8000)
    expect(reads).toBe(2)
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
