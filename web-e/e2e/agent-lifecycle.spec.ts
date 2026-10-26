import { expect, test } from '@playwright/test'

import { lifecycleReady, warningLifecycle } from './agent-lifecycle-data'
import { deferMCPRetry, prepareLifecycle, settingsPath } from './agent-lifecycle-fixture'
import { originalAgent, originalUser, replacementToken, writeFromOtherTab } from './session-fencing-fixture'

test('pending starts explain recorded state, safe failures and retry timing without claiming live health', async ({ page }) => {
  // Given
  await prepareLifecycle(page, { ...lifecycleReady, start: {
    operation_id: '33333333-3333-4333-8333-333333333333', state: 'pending', attempts: 2,
    last_attempt_at: '2030-01-01T00:00:00Z', next_retry_at: '2030-01-01T00:00:20Z', last_failure_code: 'capacity_exceeded',
  } })
  // When
  await page.goto(settingsPath)
  // Then
  const region = page.getByRole('region', { name: 'Agent lifecycle', exact: true })
  await expect(region).toContainText('Recorded runtime: Stopped')
  await expect(region).toContainText('not a live readiness check')
  await expect(region).toContainText('Start request: Pending')
  await expect(region).toContainText('2 attempts recorded')
  await expect(region).toContainText('not enough runtime capacity')
  await expect(region.locator('time[datetime="2030-01-01T00:00:20Z"]')).toBeVisible()
})

test('optional retry targets only the failed qualified server and refreshes lifecycle after pending', async ({ page }) => {
  // Given
  const api = await prepareLifecycle(page)
  const retry = await deferMCPRetry(page)
  await page.goto(settingsPath)
  const region = page.getByRole('region', { name: 'Agent lifecycle', exact: true })
  const button = region.getByRole('button', { name: 'Retry integration company/research', exact: true })
  await expect(button).toBeEnabled()
  const reads = api.reads()
  await expect(region.getByRole('button', { name: 'Integration imported company/working', exact: true })).toBeDisabled()
  try {
    // When
    await button.click({ clickCount: 2 })
    await retry.requested
    // Then
    await expect(button).toBeDisabled()
    expect(retry.bodies).toEqual([{ server_id: 'company/research' }])
    api.set({ ...warningLifecycle, optional_mcp: warningLifecycle.optional_mcp.map((item) => ({ ...item, state: 'succeeded', warning_code: null, retryable: false })) })
    retry.release()
    await expect(button).toHaveCount(0)
    await expect(region.getByText('Imported · 2 attempts', { exact: true })).toBeVisible()
    expect(api.reads()).toBeGreaterThan(reads)
  } finally { retry.release() }
})

for (const status of [409, 503]) {
  test(`optional retry ${status} leaves warnings visible with a safe recovery action`, async ({ page }) => {
    // Given
    await prepareLifecycle(page)
    const retry = await deferMCPRetry(page)
    await page.goto(settingsPath)
    try {
      // When
      await page.getByRole('button', { name: 'Retry integration company/research', exact: true }).click()
      await retry.requested
      retry.release(status)
      // Then
      await expect(page.getByRole('region', { name: 'Agent lifecycle', exact: true }).getByRole('alert')).toContainText('Unable to retry this integration')
      await expect(page.getByRole('button', { name: 'Retry integration company/research', exact: true })).toBeEnabled()
      await expect(page.getByText(/private-secret/)).toHaveCount(0)
    } finally { retry.release(status) }
  })
}

test('unavailable lifecycle fails closed and recovers through its own status check', async ({ page }) => {
  // Given
  const api = await prepareLifecycle(page)
  api.status(503)
  await page.goto(settingsPath)
  const region = page.getByRole('region', { name: 'Agent lifecycle', exact: true })
  await expect(region.getByRole('alert')).toContainText('unavailable')
  await expect(region.getByText('Policy: Eligible', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
  // When
  api.status(200)
  await region.getByRole('button', { name: 'Check lifecycle again', exact: true }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeEnabled()
  await expect(region.getByRole('button', { name: 'Retry integration company/research', exact: true })).toBeEnabled()
  await expect(page.getByText(/private-secret/)).toHaveCount(0)
})

test('mounted timestamp expiry updates the badge and disables start and optional retry at equality', async ({ page }) => {
  // Given
  const now = new Date('2030-01-01T00:00:00Z')
  await page.clock.install({ time: now })
  const api = await prepareLifecycle(page, warningLifecycle, {
    ...originalAgent, is_expired: false, expires_at: '2030-01-01T00:01:00Z',
  })
  await page.goto(settingsPath)
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeEnabled()
  await expect(page.locator('[data-slot="badge"]').filter({ hasText: /^Expired$/ })).toHaveCount(0)
  const reads = api.reads()
  await page.clock.pauseAt(new Date('2030-01-01T00:00:30Z'))
  // When
  await page.clock.runFor(30_000)
  // Then
  await expect(page.locator('[data-slot="badge"]').filter({ hasText: /^Expired$/ })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Retry integration company/research', exact: true })).toBeDisabled()
  await expect(page.getByText('Policy: Blocked', { exact: true })).toBeVisible()
  expect(api.reads()).toBeGreaterThan(reads)
})

test('policy ineligibility blocks new starts and retries without changing manual Stop', async ({ page }) => {
  // Given
  await prepareLifecycle(page, { ...warningLifecycle, availability: {
    policy_eligible: false, blocking_codes: ['owner_inactive'], can_start: false, can_retry_setup: false, can_retry_mcp: false,
  } })
  // When
  await page.goto(settingsPath)
  // Then
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Retry integration company/research', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeEnabled()
  await expect(page.getByRole('region', { name: 'Agent lifecycle', exact: true })).toContainText('creator account is inactive')
})

test('manual Start and Stop refresh the recorded lifecycle after successful updates', async ({ page }) => {
  // Given
  const api = await prepareLifecycle(page, lifecycleReady)
  let agent = originalAgent
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: agent }))
  for (const action of ['start', 'stop'] as const) await page.route(`**/api/agents/${originalAgent.id}/${action}`, (route) => {
    expect(route.request().method()).toBe('POST')
    agent = { ...agent, status: action === 'start' ? 'running' : 'stopped' }
    api.set({ ...lifecycleReady, runtime: { recorded_status: agent.status, recorded_container_id: null }, availability: {
      ...lifecycleReady.availability, blocking_codes: action === 'start' ? [] : ['runtime_stopped'], can_start: action !== 'start',
    } })
    return route.fulfill({ json: agent })
  })
  await page.goto(settingsPath)
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeEnabled()
  const reads = api.reads()
  // When
  await page.getByRole('button', { name: 'Start', exact: true }).click()
  // Then
  const region = page.getByRole('region', { name: 'Agent lifecycle', exact: true })
  await expect(region).toContainText('Recorded runtime: Running')
  expect(api.reads()).toBeGreaterThan(reads)
  const runningReads = api.reads()
  await page.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(region).toContainText('Recorded runtime: Stopped')
  expect(api.reads()).toBeGreaterThan(runningReads)
})

test('a replaced workspace clears its mounted expiry timer before the old deadline', async ({ page }) => {
  // Given
  const now = new Date('2030-01-01T00:00:00Z')
  await page.clock.install({ time: now })
  const api = await prepareLifecycle(page, lifecycleReady, { ...originalAgent, is_expired: false, expires_at: '2030-01-01T00:00:10Z' })
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: {
    ...originalAgent, expires_at: route.request().headers().authorization === `Bearer ${replacementToken}` ? null : '2030-01-01T00:00:10Z',
  } }))
  await page.goto(settingsPath)
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeEnabled()
  await writeFromOtherTab(page, replacementToken)
  await expect(page.getByRole('region', { name: 'Agent lifecycle', exact: true })).toContainText('No optional integration outcomes recorded')
  await page.clock.pauseAt(new Date('2030-01-01T00:00:05Z'))
  const reads = api.replacementReads()
  // When
  await page.clock.runFor(5000)
  // Then
  await expect(page.locator('[data-slot="badge"]').filter({ hasText: /^Expired$/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeEnabled()
  expect(api.replacementReads()).toBe(reads)
})

for (const status of [202, 503]) {
  test(`late optional retry ${status} cannot change or invalidate the replacement workspace`, async ({ page }) => {
    // Given
    const api = await prepareLifecycle(page)
    const retry = await deferMCPRetry(page)
    await page.goto(settingsPath)
    await page.getByRole('button', { name: 'Retry integration company/research', exact: true }).click()
    await retry.requested
    try {
      await writeFromOtherTab(page, replacementToken)
      await expect(page.getByRole('region', { name: 'Agent lifecycle', exact: true })).toContainText('No optional integration outcomes recorded')
      const reads = api.replacementReads()
      const completed = page.waitForEvent('requestfinished', { predicate: (request) => request.method() === 'POST' && request.url().endsWith('/setup/mcp-retry') })
      // When
      retry.release(status)
      await completed
      // Then
      await expect(page.getByRole('region', { name: 'Agent lifecycle', exact: true })).toContainText('No optional integration outcomes recorded')
      expect(api.replacementReads()).toBe(reads)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    } finally { retry.release(status) }
  })
}

for (const rights of [
  { name: 'creator', creator: true, role: 'member', access_level: 'use', is_system: false, allowed: true, manage: true },
  { name: 'org admin manager', creator: false, role: 'org_admin', access_level: 'manage', is_system: false, allowed: true, manage: true },
  { name: 'unrelated manager', creator: false, role: 'member', access_level: 'manage', is_system: false, allowed: false, manage: true },
  { name: 'use-only org admin', creator: false, role: 'org_admin', access_level: 'use', is_system: false, allowed: false, manage: false },
  { name: 'use-only member', creator: false, role: 'member', access_level: 'use', is_system: false, allowed: false, manage: false },
  { name: 'system creator', creator: true, role: 'org_admin', access_level: 'manage', is_system: true, allowed: false, manage: true },
] as const) {
  test(`lifecycle fetch and deletion rights for ${rights.name}`, async ({ page }) => {
    // Given
    const api = await prepareLifecycle(page, lifecycleReady, {
      ...originalAgent, creator_id: rights.creator ? originalUser.id : '49926b42-c074-4aa1-9aa1-54f0adbf70b9',
      access_level: rights.access_level, is_system: rights.is_system,
    })
    await page.route('**/api/member/auth/me', (route) => route.fulfill({ json: { ...originalUser, role: rights.role } }))
    // When
    await page.goto(settingsPath)
    // Then
    await expect(page.getByLabel('Name', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Request deletion', exact: true })).toHaveCount(rights.allowed ? 1 : 0)
    await expect(page.getByRole('region', { name: 'Agent lifecycle', exact: true })).toHaveCount(rights.manage ? 1 : 0)
    expect(api.reads()).toBe(rights.manage ? 1 : 0)
  })
}
