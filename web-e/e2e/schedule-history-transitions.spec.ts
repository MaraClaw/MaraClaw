import { expect, test } from '@playwright/test'

import { historyPath, historyRun } from './schedule-history-fixture'
import { prepareSchedules, savedSchedule, schedulesApiPath, schedulesPath } from './schedules-fixture'
import { originalAgent } from './session-fencing-fixture'

test('changing schedules clears accumulated history, cursor, and page errors before reopening', async ({ page }) => {
  const second = { ...savedSchedule, id: 'second-schedule', name: 'Evening report' }
  await prepareSchedules(page, [savedSchedule, second])
  const cursors: (string | null)[] = []
  await page.route(`**${historyPath}*`, (route) => {
    const cursor = new URL(route.request().url()).searchParams.get('cursor')
    cursors.push(cursor)
    if (cursor === 'older') return route.fulfill({ json: { items: [historyRun('accumulated')], next_cursor: 'unavailable' } })
    if (cursor === 'unavailable') return route.fulfill({ status: 503, json: {} })
    return route.fulfill({ json: { items: [historyRun('latest')], next_cursor: 'older' } })
  })
  await page.route(`**${schedulesApiPath}${second.id}/history/page*`, (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.goto(schedulesPath)
  const firstRow = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
  await firstRow.getByRole('button', { name: 'History', exact: true }).click()
  const history = page.getByRole('region', { name: 'Run history' })
  await history.getByRole('button', { name: 'Load older runs' }).click()
  await expect(history.getByRole('listitem')).toHaveCount(2)
  await history.getByRole('button', { name: 'Load older runs' }).click()
  await expect(history.getByRole('status')).toContainText('Unable to load older runs')

  await page.getByRole('listitem', { name: second.name, exact: true }).getByRole('button', { name: 'History', exact: true }).click()

  await expect(history.getByText('No history yet.', { exact: true })).toBeVisible()
  await expect(history.getByRole('listitem')).toHaveCount(0)
  await expect(history.getByRole('status')).toHaveCount(0)
  await firstRow.getByRole('button', { name: 'History', exact: true }).click()
  await expect(history.getByRole('listitem')).toHaveCount(1)
  await expect(history.getByText('accumulated', { exact: true })).toHaveCount(0)
  await expect(history.getByRole('button', { name: 'Load older runs' })).toBeEnabled()
  expect(cursors).toEqual([null, 'older', 'unavailable', null])
})

test('a delayed older page cannot populate a different schedule or its reopened instance', async ({ page }) => {
  const second = { ...savedSchedule, id: 'second-schedule', name: 'Evening report' }
  await prepareSchedules(page, [savedSchedule, second])
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const handled = Promise.withResolvers<void>()
  await page.route(`**${historyPath}*`, async (route) => {
    if (!new URL(route.request().url()).searchParams.has('cursor')) {
      return route.fulfill({ json: { items: [historyRun('current')], next_cursor: 'older' } })
    }
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { items: [historyRun('obsolete')], next_cursor: null } })
    handled.resolve()
  })
  await page.route(`**${schedulesApiPath}${second.id}/history/page*`, (route) => route.fulfill({ json: { items: [historyRun('second')], next_cursor: null } }))
  await page.goto(schedulesPath)
  const firstRow = page.getByRole('listitem', { name: savedSchedule.name, exact: true })
  await firstRow.getByRole('button', { name: 'History', exact: true }).click()
  const history = page.getByRole('region', { name: 'Run history' })
  await history.getByRole('button', { name: 'Load older runs' }).click()
  await requested.promise
  const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('cursor=older') })

  await page.getByRole('listitem', { name: second.name, exact: true }).getByRole('button', { name: 'History', exact: true }).click()

  await cancelled
  release.resolve()
  await handled.promise
  await expect(history.getByText('second', { exact: true })).toBeVisible()
  await expect(history.getByText('obsolete', { exact: true })).toHaveCount(0)
  await firstRow.getByRole('button', { name: 'History', exact: true }).click()
  await expect(history.getByRole('listitem')).toHaveCount(1)
  await expect(history.getByText('current', { exact: true })).toBeVisible()
})

test('agent navigation resets the selected schedule and its accumulated history', async ({ page }) => {
  const secondAgent = { ...originalAgent, id: 'second-agent', name: 'Second agent' }
  await prepareSchedules(page, [savedSchedule])
  await page.route('**/api/agents/', (route) => route.fulfill({ json: [originalAgent, secondAgent] }))
  await page.route(`**/api/agents/${secondAgent.id}`, (route) => route.fulfill({ json: secondAgent }))
  await page.route(`**/api/agents/${secondAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route(`**/api/agents/${secondAgent.id}/sessions*`, (route) => route.fulfill({ json: [] }))
  await page.route(`**/api/agents/${secondAgent.id}/schedules/`, (route) => route.fulfill({ json: [savedSchedule] }))
  await page.route(`**/api/agents/${secondAgent.id}/schedules/${savedSchedule.id}/history/page*`, (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.route(`**${historyPath}*`, (route) => route.fulfill({ json: { items: [historyRun('original-agent-only')], next_cursor: 'older' } }))
  await page.goto(schedulesPath)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  await expect(page.getByText('original-agent-only', { exact: true })).toBeVisible()

  await page.getByRole('link', { name: 'Agents', exact: true }).click()
  await page.getByRole('link').filter({ hasText: secondAgent.name }).click()
  await page.getByRole('link', { name: 'Schedules', exact: true }).click()

  await expect(page).toHaveURL(`/app/agents/${secondAgent.id}/schedules`)
  await expect(page.getByRole('region', { name: 'Run history' })).toHaveCount(0)
  await page.getByRole('button', { name: 'History', exact: true }).click()
  await expect(page.getByText('No history yet.', { exact: true })).toBeVisible()
  await expect(page.getByText('original-agent-only', { exact: true })).toHaveCount(0)
})
