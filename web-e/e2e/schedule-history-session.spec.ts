import { expect, test } from '@playwright/test'

import { historyPath, historyRun } from './schedule-history-fixture'
import { savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'
import {
  originalAgent, originalToken, originalUser, prepareWorkspace, replacementCompany,
  replacementToken, replacementUser, switchFromSettings, tokenKey, writeFromOtherTab,
} from './session-fencing-fixture'

for (const status of [200, 401, 503]) {
  test(`same-token workspace replacement fences a delayed history ${status} and resets pages and cursors`, async ({ page }) => {
    await prepareWorkspace(page, { ...originalUser, tenant_id: replacementCompany.id }, originalToken)
    await page.route('**/api/agents/', (route) => route.fulfill({ json: [originalAgent] }))
    await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
    await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
    await page.route(`**/api/agents/${originalAgent.id}/sessions*`, (route) => route.fulfill({ json: [] }))
    await page.route(`**${schedulesApiPath}`, (route) => route.fulfill({ json: [savedSchedule] }))
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const handled = Promise.withResolvers<void>()
    const cursors: (string | null)[] = []
    let replaced = false
    await page.route(`**${historyPath}*`, async (route) => {
      expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
      const cursor = new URL(route.request().url()).searchParams.get('cursor')
      cursors.push(cursor)
      if (replaced) return route.fulfill({ json: { items: [historyRun('replacement-workspace')], next_cursor: null } })
      if (cursor === null) return route.fulfill({ json: { items: [historyRun('original-latest')], next_cursor: 'older' } })
      if (cursor === 'older') return route.fulfill({ json: { items: [historyRun('original-older')], next_cursor: 'delayed' } })
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200 ? { items: [historyRun('obsolete')], next_cursor: null } : { detail: 'Retired session' } })
      handled.resolve()
    })
    await page.goto(schedulesPath)
    await page.getByRole('button', { name: 'History', exact: true }).click()
    const history = page.getByRole('region', { name: 'Run history' })
    await history.getByRole('button', { name: 'Load older runs' }).click()
    await expect(history.getByRole('listitem')).toHaveCount(2)
    await history.getByRole('button', { name: 'Load older runs' }).click()
    await requested.promise
    const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('cursor=delayed') })

    await switchFromSettings(page)

    await expect(page.getByRole('complementary').getByText(replacementCompany.name, { exact: true })).toBeVisible()
    await cancelled
    replaced = true
    release.resolve()
    await handled.promise
    await page.getByRole('link', { name: 'Agents', exact: true }).click()
    await page.getByRole('link').filter({ hasText: originalAgent.name }).click()
    await page.getByRole('link', { name: 'Schedules', exact: true }).click()
    await expect(page.getByRole('region', { name: 'Run history' })).toHaveCount(0)
    await page.getByRole('button', { name: 'History', exact: true }).click()
    await expect(history.getByRole('listitem')).toHaveCount(1)
    await expect(history.getByText('replacement-workspace', { exact: true })).toBeVisible()
    await expect(history.getByRole('status')).toHaveCount(0)
    await expect(page.getByText(/original-latest|original-older|obsolete/, { exact: true })).toHaveCount(0)
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
    expect(cursors).toEqual([null, 'older', 'delayed', null])
  })
}

test('cross-tab session replacement clears history errors and fences a late unauthorized page', async ({ page }) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route(`**${schedulesApiPath}`, (route) => route.fulfill({ json: [savedSchedule] }))
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const handled = Promise.withResolvers<void>()
  let attempts = 0
  await page.route(`**${historyPath}*`, async (route) => {
    if (++attempts === 1) return route.fulfill({ status: 503, json: {} })
    requested.resolve()
    await release.promise
    await route.fulfill({ status: 401, json: { detail: 'Retired history owner' } })
    handled.resolve()
  })
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('Unable to load run history')
  await page.getByRole('button', { name: 'Retry history' }).click()
  await requested.promise
  const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('/history/page') })

  await writeFromOtherTab(page, replacementToken)

  await cancelled
  await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
  await expect(page.getByLabel('Display name')).toHaveValue(replacementUser.display_name ?? '')
  release.resolve()
  await handled.promise
  await expect(page).toHaveURL(/\/app\/settings$/)
  await expect(page.getByRole('region', { name: 'Run history' })).toHaveCount(0)
  await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
})

test('same-token cross-tab reissuance resets mounted history and rejects its obsolete page', async ({ page }) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route(`**${schedulesApiPath}`, (route) => route.fulfill({ json: [savedSchedule] }))
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const handled = Promise.withResolvers<void>()
  const cursors: (string | null)[] = []
  await page.route(`**${historyPath}*`, async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get('cursor')
    cursors.push(cursor)
    if (cursor === null) return route.fulfill({ json: { items: [historyRun('current-session')], next_cursor: 'older' } })
    requested.resolve()
    await release.promise
    await route.fulfill({ status: 401, json: { detail: 'Obsolete owner' } })
    handled.resolve()
  })
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  await page.getByRole('button', { name: 'Load older runs' }).click()
  await requested.promise
  const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('cursor=older') })
  const peer = await page.context().newPage()
  try {
    await peer.route('**/schedule-history-peer', (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>History session peer</title>' }))
    await peer.goto('/schedule-history-peer')

    await peer.evaluate(({ tokenKey, originalToken }) => {
      localStorage.removeItem(tokenKey)
      localStorage.setItem(tokenKey, originalToken)
    }, { tokenKey, originalToken })

    await cancelled
    await expect(page.getByRole('region', { name: 'Run history' })).toHaveCount(0)
    release.resolve()
    await handled.promise
    await page.getByRole('button', { name: 'History', exact: true }).click()
    await expect(page.getByText('current-session', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Load older runs' })).toBeEnabled()
    expect(cursors).toEqual([null, 'older', null])
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
  } finally {
    await peer.close()
  }
})
