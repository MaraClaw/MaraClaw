import { expect, test } from '@playwright/test'

import { historyPath, historyRun } from './schedule-history-fixture'
import { prepareSchedules, savedSchedule, schedulesPath } from './schedules-fixture'
import { originalToken, replacementToken, tokenKey, writeFromOtherTab } from './session-fencing-fixture'

test('keyboard filtering resets accumulated pages and keeps initial, empty and older-retry states distinct', async ({ page }) => {
  // Given: each filter has an independent cursor chain.
  await prepareSchedules(page, [savedSchedule])
  const calls: { outcome: string | null; cursor: string | null }[] = []
  let failures = 0
  let olderAttempts = 0
  await page.route(`**${historyPath}*`, async (route) => {
    const query = new URL(route.request().url()).searchParams
    const outcome = query.get('outcome')
    const cursor = query.get('cursor')
    calls.push({ outcome, cursor })
    if (outcome === 'failure') return route.fulfill(++failures === 1
      ? { status: 503, json: {} } : { json: { items: [], next_cursor: null } })
    if (outcome === 'unknown' && cursor) return route.fulfill(++olderAttempts === 1
      ? { status: 503, json: {} } : { json: { items: [historyRun('unknown-older')], next_cursor: null } })
    await route.fulfill({ json: {
      items: [historyRun(`${outcome}-${cursor ? 'older' : 'latest'}`)], next_cursor: cursor ? null : `${outcome}/+ cursor`,
    } })
  })
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  const history = page.getByRole('region', { name: 'Run history' })
  await history.getByRole('button', { name: 'Load older runs' }).click()
  await expect(history.getByRole('listitem')).toHaveCount(2)
  const filter = history.getByRole('combobox', { name: 'Outcome' })
  // When: keyboard selection changes the outcome.
  await filter.focus()
  await filter.press('f')
  await filter.press('Enter')
  // Then: old rows/cursors are gone, and first-page retry is not an older-page retry.
  await expect(filter).toHaveValue('failure')
  await expect(filter).toBeFocused()
  await expect(history.getByRole('listitem')).toHaveCount(0)
  await expect(history.getByRole('button', { name: 'Load older runs' })).toHaveCount(0)
  await history.getByRole('button', { name: 'Retry history' }).click()
  await expect(history.getByText('No runs with this outcome.')).toBeVisible()
  await expect(history.getByText('No history yet.')).toHaveCount(0)
  await filter.selectOption('unknown')
  await expect(history.getByText('unknown-latest', { exact: true })).toBeVisible()
  await history.getByRole('button', { name: 'Load older runs' }).click()
  await expect(history.getByText('unknown-latest', { exact: true })).toBeVisible()
  await history.getByRole('button', { name: 'Retry older runs' }).click()
  await expect(history.getByRole('listitem')).toHaveCount(2)
  await filter.selectOption('all')
  await expect(history.getByRole('listitem')).toHaveCount(1)
  expect(calls).toEqual([
    { outcome: 'all', cursor: null }, { outcome: 'all', cursor: 'all/+ cursor' },
    { outcome: 'failure', cursor: null }, { outcome: 'failure', cursor: null },
    { outcome: 'unknown', cursor: null }, { outcome: 'unknown', cursor: 'unknown/+ cursor' },
    { outcome: 'unknown', cursor: 'unknown/+ cursor' }, { outcome: 'all', cursor: null },
  ])
})

for (const status of [200, 401, 503]) {
  test(`switching filters fences a delayed older ${status}, including returning to the original filter`, async ({ page }) => {
    // Given
    await prepareSchedules(page, [savedSchedule])
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const handled = Promise.withResolvers<void>()
    let allLoads = 0
    await page.route(`**${historyPath}*`, async (route) => {
      const query = new URL(route.request().url()).searchParams
      if (!query.has('cursor')) {
        const outcome = query.get('outcome')
        if (outcome === 'all') allLoads++
        return route.fulfill({ json: { items: [historyRun(`${outcome}-fresh`)], next_cursor: outcome === 'all' ? 'obsolete' : null } })
      }
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200 ? { items: [historyRun('obsolete-filter-run')], next_cursor: null } : { detail: 'Obsolete filter' } })
      handled.resolve()
    })
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'History', exact: true }).click()
    const history = page.getByRole('region', { name: 'Run history' })
    await history.getByRole('button', { name: 'Load older runs' }).click()
    await requested.promise
    const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('cursor=obsolete') })
    // When
    await history.getByLabel('Outcome').selectOption('failure')
    await cancelled
    await expect(history.getByText('failure-fresh', { exact: true })).toBeVisible()
    await history.getByLabel('Outcome').selectOption('all')
    release.resolve()
    await handled.promise
    // Then
    await expect(history.getByText('all-fresh', { exact: true })).toBeVisible()
    await expect(history.getByRole('listitem')).toHaveCount(1)
    await expect(history.getByRole('status')).toHaveCount(0)
    expect(allLoads).toBe(2)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
  })
}

test('a filtered page cannot expire or populate a replacement session', async ({ page }) => {
  // Given
  await prepareSchedules(page, [savedSchedule])
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const handled = Promise.withResolvers<void>()
  await page.route(`**${historyPath}*`, async (route) => {
    const query = new URL(route.request().url()).searchParams
    if (query.get('outcome') === 'all') return route.fulfill({ json: { items: [], next_cursor: null } })
    requested.resolve()
    await release.promise
    await route.fulfill({ status: 401, json: { detail: 'Obsolete filtered session' } })
    handled.resolve()
  })
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  await page.getByLabel('Outcome').selectOption('cancelled')
  await requested.promise
  const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('outcome=cancelled') })
  // When
  await writeFromOtherTab(page, replacementToken)
  await cancelled
  await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
  release.resolve()
  await handled.promise
  // Then
  await expect(page).toHaveURL('/app/settings')
  await expect(page.getByRole('region', { name: 'Run history' })).toHaveCount(0)
  await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
})

test('changing the filter during its first page excludes the delayed initial response', async ({ page }) => {
  // Given
  await prepareSchedules(page, [savedSchedule])
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const handled = Promise.withResolvers<void>()
  await page.route(`**${historyPath}*`, async (route) => {
    if (new URL(route.request().url()).searchParams.get('outcome') === 'success') {
      return route.fulfill({ json: { items: [historyRun('current-success', 'success')], next_cursor: null } })
    }
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { items: [historyRun('obsolete-first-page')], next_cursor: 'obsolete' } })
    handled.resolve()
  })
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  await requested.promise
  const history = page.getByRole('region', { name: 'Run history' })
  const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('outcome=all') })
  // When
  await history.getByLabel('Outcome').selectOption('success')
  await cancelled
  release.resolve()
  await handled.promise
  // Then
  await expect(history.getByText('current-success', { exact: true })).toBeVisible()
  await expect(history.getByRole('listitem')).toHaveCount(1)
  await expect(history.getByRole('button')).toHaveCount(0)
  await expect(history.getByText('obsolete-first-page')).toHaveCount(0)
})
