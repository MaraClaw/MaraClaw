import { expect, test } from '@playwright/test'

import { activity, activityApiPath, activityPath, assertActivityRequest, prepareActivity } from './activity-fixture'
import { originalAgent } from './session-fencing-fixture'

for (const role of ['member', 'org_admin']) {
  test(`direct Activity navigation denies use-only ${role} without a history request or loading rail leak`, async ({ page }) => {
    // Given
    await prepareActivity(page, 'use', role)
    const release = Promise.withResolvers<void>()
    await page.route(`**/api/agents/${originalAgent.id}`, async (route) => {
      await release.promise
      return route.fulfill({ json: { ...originalAgent, access_level: 'use' } })
    })
    let requests = 0
    await page.route(`**${activityApiPath}*`, (route) => { requests++; return route.fulfill({ json: { items: [activity], next_cursor: null } }) })
    // When
    await page.goto(activityPath)
    // Then
    await expect(page.getByRole('navigation', { name: 'Agent sections' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Activity', exact: true })).toHaveCount(0)
    release.resolve()
    await expect(page.getByText('Only agent managers can view activity. Ask a manager for access.')).toBeVisible()
    await expect(page.getByRole('link', { name: 'Activity', exact: true })).toHaveCount(0)
    expect(requests).toBe(0)
  })
}

test('manager filters by exact unknown action type and restarts traversal without refetching the outlet agent', async ({ page }) => {
  // Given
  await prepareActivity(page)
  let agentReads = 0
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => { agentReads++; return route.fulfill({ json: originalAgent }) })
  const queries: [string | null, string | null][] = []
  const filter = ' Tool + / Exact '
  await page.route(`**${activityApiPath}*`, (route) => {
    assertActivityRequest(route.request())
    const params = new URL(route.request().url()).searchParams
    queries.push([params.get('action_type'), params.get('cursor')])
    return route.fulfill({ json: {
      items: [{ ...activity, action_type: filter, summary: params.has('action_type') ? 'Filtered action' : activity.summary,
        detail_json: 'never rendered', related_id: 'private related record' }], next_cursor: 'opaque+/=? older',
    } })
  })
  await page.goto(activityPath)
  await expect(page.getByText(activity.summary, { exact: true })).toBeVisible()
  await expect(page.getByText('Time unavailable', { exact: true })).toBeVisible()
  await page.getByLabel('Action type (exact match)').fill(filter)
  // When
  await page.getByRole('button', { name: 'Apply filter', exact: true }).press('Enter')
  // Then
  await expect(page.getByText('Filtered action', { exact: true })).toBeVisible()
  await expect(page.getByText(activity.summary, { exact: true })).toHaveCount(0)
  await expect(page.getByText('never rendered', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Load older activity', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Load older activity', exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Clear filter', exact: true }).click()
  await expect(page.getByText(activity.summary, { exact: true })).toBeVisible()
  expect(queries).toEqual([[null, null], [filter, null], [filter, 'opaque+/=? older'], [null, null]])
  expect(agentReads).toBe(1)
})

test('older-page failure retains loaded rows and retries only the encoded cursor', async ({ page }) => {
  // Given
  await prepareActivity(page)
  const cursors: (string | null)[] = []
  await page.route(`**${activityApiPath}*`, (route) => {
    const cursor = new URL(route.request().url()).searchParams.get('cursor')
    cursors.push(cursor)
    if (!cursor) return route.fulfill({ json: { items: [activity], next_cursor: 'opaque+/=? older' } })
    if (cursors.length === 2) return route.fulfill({ status: 503, json: { detail: 'private backend error' } })
    return route.fulfill({ json: { items: [{ ...activity, id: 'older', summary: 'Older action', created_at: '2026-10-29T00:00:00Z' }], next_cursor: null } })
  })
  await page.goto(activityPath)
  await expect(page.getByText(activity.summary, { exact: true })).toBeVisible()
  // When
  await page.getByRole('button', { name: 'Load older activity', exact: true }).click()
  // Then
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.getByText(activity.summary, { exact: true })).toBeVisible()
  await expect(page.getByText('private backend error')).toHaveCount(0)
  await page.getByRole('button', { name: 'Retry older activity', exact: true }).click()
  await expect(page.getByRole('list', { name: 'Agent activity' }).getByRole('listitem')).toHaveCount(2)
  await expect(page.getByText('Older action', { exact: true })).toBeVisible()
  expect(cursors).toEqual([null, 'opaque+/=? older', 'opaque+/=? older'])
})

test('initial malformed response retries to usable empty history', async ({ page }) => {
  // Given
  await prepareActivity(page)
  let calls = 0
  await page.route(`**${activityApiPath}*`, (route) => route.fulfill({ json: ++calls === 1
    ? { items: [{ ...activity, summary: null }], next_cursor: null } : { items: [], next_cursor: null } }))
  await page.goto(activityPath)
  await expect(page.getByRole('alert')).toBeVisible()
  // When
  await page.getByRole('button', { name: 'Retry activity', exact: true }).click()
  // Then
  await expect(page.getByRole('status').filter({ hasText: 'No activity found.' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Refresh activity', exact: true })).toBeEnabled()
  expect(calls).toBe(2)
})

test('string timestamps keep summaries usable with a safe fallback for unparseable time', async ({ page }, testInfo) => {
  // Given
  await prepareActivity(page)
  await page.route(`**${activityApiPath}*`, (route) => route.fulfill({ json: { items: [
    { ...activity, id: 'legacy', summary: 'Legacy timestamp action', created_at: '2026-10-30T00:00:00' },
    { ...activity, id: 'unparseable', summary: 'Unparseable timestamp action', created_at: 'unavailable legacy time' },
  ], next_cursor: null } }))
  // When
  await page.goto(activityPath)
  // Then
  const history = page.getByRole('list', { name: 'Agent activity' })
  await expect(history.getByRole('listitem')).toHaveCount(2)
  await expect(history.getByText('Legacy timestamp action', { exact: true })).toBeVisible()
  await expect(history.getByText('Unparseable timestamp action', { exact: true })).toBeVisible()
  await expect(history.locator('time')).toHaveCount(1)
  await expect(history.getByText('Time unavailable', { exact: true })).toHaveCount(1)
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('timestamp-contract.png') })
})

test('older paging keeps a bounded 100-row window and refresh returns to latest', async ({ page }) => {
  // Given
  await prepareActivity(page)
  await page.route(`**${activityApiPath}*`, (route) => {
    const index = Number(new URL(route.request().url()).searchParams.get('cursor') ?? 0)
    return route.fulfill({ json: { items: Array.from({ length: 20 }, (_, row) => ({ ...activity, id: `${index}-${row}`, summary: `Action ${index}-${row}` })), next_cursor: String(index + 1) } })
  })
  await page.goto(activityPath)
  // When
  for (let index = 0; index < 6; index++) {
    await expect(page.getByRole('list', { name: 'Agent activity' }).getByRole('listitem')).toHaveCount(Math.min(index + 1, 5) * 20)
    await page.getByRole('button', { name: 'Load older activity', exact: true }).click()
  }
  // Then
  await expect(page.getByRole('list', { name: 'Agent activity' }).getByRole('listitem')).toHaveCount(100)
  await expect(page.getByText('Action 0-0', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('status').filter({ hasText: 'Showing up to 100' })).toBeVisible()
  await page.getByRole('button', { name: 'Refresh activity', exact: true }).click()
  await expect(page.getByText('Action 0-0', { exact: true })).toBeVisible()
  await expect(page.getByRole('list', { name: 'Agent activity' }).getByRole('listitem')).toHaveCount(20)
})

test('fresh 403 removes sensitive filter history and the stale manager rail grant', async ({ page }) => {
  // Given
  await prepareActivity(page)
  let calls = 0
  await page.route(`**${activityApiPath}*`, (route) => {
    calls++
    const params = new URL(route.request().url()).searchParams
    if (params.has('cursor')) return route.fulfill({ status: 403, json: { detail: 'private denial detail' } })
    return route.fulfill({ json: { items: [{ ...activity, summary: params.has('action_type') ? 'Sensitive filtered action' : activity.summary }], next_cursor: 'revoke-next' } })
  })
  await page.goto(activityPath)
  await expect(page.getByText(activity.summary, { exact: true })).toBeVisible()
  await page.getByLabel('Action type (exact match)').fill('tool_call')
  await page.getByRole('button', { name: 'Apply filter', exact: true }).click()
  await expect(page.getByText('Sensitive filtered action', { exact: true })).toBeVisible()
  // When
  await page.getByRole('button', { name: 'Load older activity', exact: true }).click()
  // Then
  await expect(page.getByText('Only agent managers can view activity. Ask a manager for access.')).toBeVisible()
  await expect(page.getByRole('link', { name: 'Activity', exact: true })).toHaveCount(0)
  await expect(page.getByRole('list', { name: 'Agent activity' })).toHaveCount(0)
  await expect(page.getByText('Sensitive filtered action', { exact: true })).toHaveCount(0)
  await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
  await page.goBack()
  await expect(page.getByText('Only agent managers can view activity. Ask a manager for access.')).toBeVisible()
  expect(calls).toBe(3)
})

test('OpenClaw checking and unavailable gates never mount Activity requests', async ({ page }) => {
  // Given
  await prepareActivity(page)
  const release = Promise.withResolvers<void>()
  await page.route(`**/api/agents/${originalAgent.id}/openclaw-update`, async (route) => {
    await release.promise
    return route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
  })
  let calls = 0
  await page.route(`**${activityApiPath}*`, (route) => { calls++; return route.fulfill({ json: { items: [], next_cursor: null } }) })
  // When
  await page.goto(activityPath)
  // Then
  await expect(page.getByRole('heading', { name: 'Agent work is paused' })).toBeVisible()
  expect(calls).toBe(0)
  release.resolve()
  await expect(page.getByRole('button', { name: 'Retry status check', exact: true })).toBeVisible()
  expect(calls).toBe(0)
})
