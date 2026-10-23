import { expect, test } from '@playwright/test'

import { delayedAgent, originalAgent } from './session-fencing-fixture'
import {
  assertHistoryRequest, historyExecution, historyPath, latestId, olderId, prepareTriggerHistory,
  primaryTrigger, secondaryHistoryPath, secondaryTrigger, tasksPath,
} from './trigger-executions-fixture'

for (const [access, role, eligible] of [['manage', 'member', true], ['use', 'org_admin', true], ['use', 'member', false]] as const) {
  test(`${role} with ${access} access ${eligible ? 'can read' : 'cannot request'} trigger history`, async ({ page }) => {
    // Given
    await prepareTriggerHistory(page, access, role)
    const calls: string[] = []
    await page.route(`**${historyPath}*`, (route) => {
      calls.push(route.request().url())
      assertHistoryRequest(route.request().url(), route.request().method(), route.request().headers().authorization)
      return route.fulfill({ json: { items: [historyExecution()], next_cursor: null } })
    })
    // When
    await page.goto(tasksPath)
    // Then
    await expect(page.getByText(primaryTrigger.name, { exact: true })).toBeVisible()
    const disclosure = page.getByRole('button', { name: `Show execution history for ${primaryTrigger.name}` })
    await expect(disclosure).toHaveCount(eligible ? 1 : 0)
    expect(calls).toHaveLength(0)
    if (eligible) {
      await disclosure.focus()
      await page.keyboard.press('Enter')
      await expect(page.getByRole('button', { name: `Hide execution history for ${primaryTrigger.name}` })).toHaveAttribute('aria-expanded', 'true')
      await expect(page.getByRole('region', { name: `${primaryTrigger.name} execution history` }).getByRole('listitem')).toHaveCount(1)
      expect(calls).toHaveLength(1)
    }
    await expect(page.getByRole('button', { name: 'Delete', exact: true })).toHaveCount(2)
  })
}

test('pagination keeps loaded executions through a generic additional-page failure and retries the same cursor', async ({ page }) => {
  // Given
  await prepareTriggerHistory(page)
  const cursors: (string | null)[] = []
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**${historyPath}*`, async (route) => {
    const request = route.request()
    assertHistoryRequest(request.url(), request.method(), request.headers().authorization)
    const cursor = new URL(request.url()).searchParams.get('cursor')
    cursors.push(cursor)
    if (cursor === null) return route.fulfill({ json: { items: [historyExecution()], next_cursor: 'opaque+/=? older' } })
    if (cursors.length === 2) {
      requested.resolve()
      await release.promise
      return route.fulfill({ status: 503, json: { detail: 'private payload failure' } })
    }
    return route.fulfill({ json: { items: [historyExecution(olderId, 'succeeded')], next_cursor: null } })
  })
  await page.goto(tasksPath)
  await page.getByRole('button', { name: `Show execution history for ${primaryTrigger.name}` }).click()
  const history = page.getByRole('region', { name: `${primaryTrigger.name} execution history` })
  await expect(history.getByRole('listitem')).toHaveCount(1)
  // When
  await history.getByRole('button', { name: 'Load older executions', exact: true }).click()
  await requested.promise
  // Then
  await expect(history.getByRole('button', { name: 'Loading older executions…', exact: true })).toBeDisabled()
  await expect(history.getByRole('listitem')).toHaveCount(1)
  release.resolve()
  await expect(history.getByRole('alert')).toBeVisible()
  await expect(page.getByText('private payload failure')).toHaveCount(0)
  await expect(history.getByRole('listitem')).toHaveCount(1)
  const retry = history.getByRole('button', { name: 'Retry older executions', exact: true })
  expect(await retry.evaluate((button) => {
    const alert = button.previousElementSibling
    const list = button.closest('section')?.querySelector('ul')
    return alert?.getAttribute('role') === 'alert'
      && Boolean(list && (list.compareDocumentPosition(alert) & Node.DOCUMENT_POSITION_FOLLOWING))
  })).toBe(true)
  await retry.click()
  await expect(history.getByRole('listitem')).toHaveCount(2)
  await expect(history.getByRole('button', { name: /older executions/ })).toHaveCount(0)
  await history.locator('summary').first().click()
  await expect(history.getByText(latestId, { exact: true })).toBeVisible()
  await expect(history.locator('time').first()).toHaveAttribute('datetime', '2026-10-23T08:00:00+08:00')
  expect(cursors).toEqual([null, 'opaque+/=? older', 'opaque+/=? older'])
})

test('initial loading and malformed-response failure recover to empty history without exposing details', async ({ page }) => {
  // Given
  await prepareTriggerHistory(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  let calls = 0
  await page.route(`**${historyPath}*`, async (route) => {
    if (++calls > 1) return route.fulfill({ json: { items: [], next_cursor: null } })
    requested.resolve()
    await release.promise
    return route.fulfill({ json: { items: [{ ...historyExecution(), created_at: 'private invalid date' }], next_cursor: null } })
  })
  await page.goto(tasksPath)
  // When
  await page.getByRole('button', { name: `Show execution history for ${primaryTrigger.name}` }).click()
  await requested.promise
  // Then
  const history = page.getByRole('region', { name: `${primaryTrigger.name} execution history` })
  await expect(history.getByRole('status')).toContainText('Loading')
  release.resolve()
  await expect(history.getByRole('alert')).toBeVisible()
  await expect(page.getByText('private invalid date')).toHaveCount(0)
  await history.getByRole('button', { name: 'Retry history', exact: true }).click()
  await expect(history.getByRole('status')).toContainText('No executions')
  await expect(history.getByRole('listitem')).toHaveCount(0)
  await history.getByRole('button', { name: 'Refresh history', exact: true }).click()
  await expect.poll(() => calls).toBe(3)
})

test('switching triggers cannot flash rows or inherit a cursor from another disclosure', async ({ page }) => {
  // Given
  await prepareTriggerHistory(page)
  await page.route(`**${historyPath}*`, (route) => route.fulfill({ json: { items: [historyExecution()], next_cursor: 'primary-only' } }))
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**${secondaryHistoryPath}*`, async (route) => {
    expect(new URL(route.request().url()).searchParams.get('cursor')).toBeNull()
    requested.resolve()
    await release.promise
    return route.fulfill({ json: { items: [], next_cursor: null } })
  })
  await page.goto(tasksPath)
  await page.getByRole('button', { name: `Show execution history for ${primaryTrigger.name}` }).click()
  await expect(page.getByRole('list', { name: 'Trigger executions' })).toBeVisible()
  await page.getByRole('button', { name: `Hide execution history for ${primaryTrigger.name}` }).click()
  // When
  await page.getByRole('button', { name: `Show execution history for ${secondaryTrigger.name}` }).click()
  await requested.promise
  // Then
  const history = page.getByRole('region', { name: `${secondaryTrigger.name} execution history` })
  await expect(history.getByRole('status')).toContainText('Loading')
  await expect(page.getByRole('list', { name: 'Trigger executions' })).toHaveCount(0)
  release.resolve()
  await expect(history.getByRole('status')).toContainText('No executions')
})

test('read-only history wraps at phone width and keeps exact identity values selectable', async ({ page }) => {
  // Given
  await page.setViewportSize({ width: 390, height: 844 })
  await prepareTriggerHistory(page)
  await page.route(`**${historyPath}*`, (route) => route.fulfill({ json: { items: [{
    ...historyExecution(latestId, 'awaiting_external_confirmation'), source: 'persisted-source-with-a-long-name',
  }], next_cursor: null } }))
  await page.goto(tasksPath)
  // When
  await page.getByRole('button', { name: `Show execution history for ${primaryTrigger.name}` }).click()
  // Then
  const history = page.getByRole('region', { name: `${primaryTrigger.name} execution history` })
  await expect(history.getByText('Awaiting External Confirmation', { exact: true })).toBeVisible()
  await history.locator('summary').click()
  await expect(history.getByText(originalAgent.id, { exact: true })).toBeVisible()
  expect(await history.locator('dl').evaluate((node) => getComputedStyle(node).userSelect)).toBe('text')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(await history.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true)
  await expect(history.getByRole('button', { name: /^(Run|Retry|Cancel)$/ })).toHaveCount(0)
})

test('navigating to another agent starts its history without rows or cursors from the previous agent', async ({ page }) => {
  // Given
  await prepareTriggerHistory(page)
  await page.route('**/api/agents/', (route) => route.fulfill({ json: [originalAgent, delayedAgent] }))
  await page.route(`**/api/agents/${delayedAgent.id}/openclaw-update`, (route) => route.fulfill({
    json: { notice: null, run: null, required: false, can_upgrade: false },
  }))
  for (const path of ['tasks/', 'focus/']) {
    await page.route(`**/api/agents/${delayedAgent.id}/${path}`, (route) => route.fulfill({ json: [] }))
  }
  await page.route(`**/api/agents/${delayedAgent.id}/triggers`, (route) => route.fulfill({ json: [primaryTrigger] }))
  await page.route(`**${historyPath}*`, (route) => route.fulfill({ json: { items: [historyExecution()], next_cursor: 'first-agent-only' } }))
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**/api/agents/${delayedAgent.id}/triggers/${primaryTrigger.id}/executions*`, async (route) => {
    expect(new URL(route.request().url()).searchParams.get('cursor')).toBeNull()
    requested.resolve()
    await release.promise
    return route.fulfill({ json: { items: [], next_cursor: null } })
  })
  await page.goto(tasksPath)
  await page.getByRole('button', { name: `Show execution history for ${primaryTrigger.name}` }).click()
  await expect(page.getByRole('list', { name: 'Trigger executions' })).toBeVisible()
  // When
  await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
  await page.getByRole('link', { name: new RegExp(delayedAgent.name) }).click()
  await page.getByRole('link', { name: 'Tasks', exact: true }).click()
  // Then
  await expect(page.getByRole('list', { name: 'Trigger executions' })).toHaveCount(0)
  const disclosure = page.getByRole('button', { name: `Show execution history for ${primaryTrigger.name}` })
  await expect(disclosure).toHaveAttribute('aria-expanded', 'false')
  await disclosure.click()
  await requested.promise
  await expect(page.getByRole('status')).toContainText('Loading execution history')
  await expect(page.getByRole('list', { name: 'Trigger executions' })).toHaveCount(0)
  release.resolve()
  await expect(page.getByRole('status')).toContainText('No executions')
})
