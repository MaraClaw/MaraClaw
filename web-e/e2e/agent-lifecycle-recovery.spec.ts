import { expect, test, type Page } from '@playwright/test'

import type { AgentLifecycle } from '../src/lib/agent-lifecycle-api'
import type { AgentOut } from '../src/lib/workspace-api'
import { lifecycleReady, warningLifecycle } from './agent-lifecycle-data'
import { deferMCPRetry, lifecyclePath, prepareLifecycle, settingsPath } from './agent-lifecycle-fixture'
import { originalAgent, replacementUser } from './session-fencing-fixture'

const loss = { reason: 'container_missing', detected_at: '2030-01-01T00:10:00Z' } as const
const healthyAgent: AgentOut = { ...originalAgent, status: 'running', runtime_loss: null }
const lostAgent: AgentOut = { ...healthyAgent, runtime_loss: loss }
const healthyLifecycle: AgentLifecycle = {
  ...lifecycleReady, runtime: { recorded_status: 'running', recorded_container_id: 'guest-container' },
  availability: { ...lifecycleReady.availability, blocking_codes: [] },
}
// Dispatch records a loss only for a running agent and leaves its recorded status, so availability is unchanged.
const lostLifecycle: AgentLifecycle = { ...healthyLifecycle, runtime: { ...healthyLifecycle.runtime, loss } }
const upgradeInProgress = {
  notice: null, run: { id: '0b6f4c4e-2d1a-4c8e-9a3b-5f6d7e8f9a0b', state: 'cutover', failure_code: null },
  required: false, can_upgrade: false,
}

// TanStack's focus manager listens for visibilitychange on window; it refetches stale focus-refetching queries.
async function returnToPage(page: Page) {
  await page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')))
}

for (const width of [390, 1440]) {
  test(`integration recovery preserves following controls at ${width}px`, async ({ page }) => {
    // Given
    await page.setViewportSize({ width, height: 900 })
    const api = await prepareLifecycle(page)
    const retry = await deferMCPRetry(page)
    await page.goto(settingsPath)
    const region = page.getByRole('region', { name: 'Agent lifecycle', exact: true })
    const button = region.getByRole('button', { name: 'Retry integration company/research', exact: true })
    await expect(button).toBeEnabled()
    const geometry = () => page.evaluate(() => {
      const scroller = document.querySelector('section[aria-label="Agent lifecycle"]')?.closest('.overflow-y-auto')
      return [document.querySelector('input#hb-hours'), [...document.querySelectorAll('button')].find((item) => item.getAttribute('aria-label') === 'Start')]
        .map((item) => item ? item.getBoundingClientRect().top + (scroller?.scrollTop ?? 0) : null)
    })
    const before = await geometry()
    // When
    await button.click()
    await retry.requested
    api.set({ ...warningLifecycle, optional_mcp: warningLifecycle.optional_mcp.map((item) => ({ ...item, state: 'succeeded', warning_code: null, retryable: false })) })
    retry.release()
    // Then
    await expect(region.getByRole('button', { name: 'Integration imported company/research', exact: true })).toBeDisabled()
    expect(await geometry()).toEqual(before)
    expect(retry.bodies).toEqual([{ server_id: 'company/research' }])
  })

  test(`initial lifecycle result preserves the reserved footprint at ${width}px`, async ({ page }) => {
    // Given
    await page.setViewportSize({ width, height: 900 })
    await prepareLifecycle(page, lifecycleReady)
    const release = Promise.withResolvers<void>()
    await page.route(lifecyclePath, async (route) => { await release.promise; await route.fallback() })
    await page.goto(settingsPath)
    const region = page.getByRole('region', { name: 'Agent lifecycle', exact: true })
    await expect(region).toContainText('Checking recorded lifecycle status')
    await expect(page.getByRole('region', { name: 'OpenClaw upgrade' })).toContainText('up to date')
    const before = await region.evaluate((item) => item.getBoundingClientRect().height)
    // When
    release.resolve()
    // Then
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeEnabled()
    expect(await region.evaluate((item) => item.getBoundingClientRect().height)).toBe(before)
  })
}

test('offline status fails closed and reconnect refreshes the lifecycle without a manual reload', async ({ page }) => {
  // Given
  const api = await prepareLifecycle(page)
  await page.goto(settingsPath)
  const start = page.getByRole('button', { name: 'Start', exact: true })
  const retry = page.getByRole('button', { name: 'Retry integration company/research', exact: true })
  await expect(start).toBeEnabled()
  const reads = api.reads()
  // When
  await page.context().setOffline(true)
  // Then
  const region = page.getByRole('region', { name: 'Agent lifecycle', exact: true })
  await expect(region).toContainText('You are offline')
  await expect(start).toBeDisabled()
  await expect(retry).toBeDisabled()
  await expect(region).toContainText('Policy: Unconfirmed')
  await page.context().setOffline(false)
  await expect(start).toBeEnabled()
  await expect(retry).toBeEnabled()
  expect(api.reads()).toBeGreaterThan(reads)
})

test('a retry paused by a connection loss announces its state and resumes only on reconnect', async ({ page }) => {
  // Given
  await prepareLifecycle(page)
  const request = await deferMCPRetry(page)
  await page.goto(settingsPath)
  const retry = page.getByRole('button', { name: 'Retry integration company/research', exact: true })
  await expect(retry).toBeEnabled()
  await retry.evaluate((button) => button.addEventListener('click', () => window.dispatchEvent(new Event('offline')), { capture: true, once: true }))
  // When
  await retry.click()
  // Then
  await expect(page.getByRole('region', { name: 'Agent lifecycle', exact: true })).toContainText('Integration retry is paused while offline')
  expect(request.bodies).toEqual([])
  await expect(retry).toBeDisabled()
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await request.requested
  expect(request.bodies).toEqual([{ server_id: 'company/research' }])
  request.release()
  await expect(retry).toBeEnabled()
})

for (const blocked of ['expired', 'setup_pending', 'upgrade_in_progress'] as const) {
  test(`stopped runtime does not recommend a disabled Start when ${blocked}`, async ({ page }) => {
    // Given
    await prepareLifecycle(page, { ...lifecycleReady, availability: { ...lifecycleReady.availability,
      can_start: false, blocking_codes: ['runtime_stopped', blocked],
    } }, { ...originalAgent, is_expired: blocked === 'expired' })
    // When
    await page.goto(settingsPath)
    // Then
    const region = page.getByRole('region', { name: 'Agent lifecycle', exact: true })
    await expect(region).toContainText('The recorded runtime is stopped.')
    await expect(region.getByText('Use Start to request a start.', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
  })
}

for (const upgrading of [false, true]) {
  test(`a container loss found while Settings is open shows a${upgrading ? ' disabled' : 'n enabled'} recovery without a reload${upgrading ? ' during an upgrade' : ''}`, async ({ page }) => {
    // Given
    const api = await prepareLifecycle(page, healthyLifecycle, healthyAgent)
    if (upgrading) await page.route(`**/api/agents/${originalAgent.id}/openclaw-update`, (route) => route.fulfill({ json: upgradeInProgress }))
    await page.goto(settingsPath)
    await expect(page.getByRole('region', { name: 'Agent lifecycle', exact: true })).toContainText('Recorded runtime: Running')
    await page.evaluate(() => { document.documentElement.dataset.sameDocument = 'yes' })
    const reads = api.reads()
    // When
    api.setAgent(lostAgent)
    api.set(lostLifecycle)
    await returnToPage(page)
    // Then
    const dialog = page.getByRole('alertdialog', { name: 'This agent’s workspace stopped running', exact: true })
    const recover = dialog.getByRole('button', { name: 'Start a new container', exact: true })
    await expect(dialog).toContainText('The container this agent runs in is no longer available')
    if (upgrading) {
      await expect(recover).toBeDisabled()
      await expect(recover).toHaveAccessibleDescription('An upgrade is in progress. Wait for it to finish.')
    } else {
      await expect(recover).toBeEnabled()
    }
    expect(api.reads()).toBeGreaterThan(reads)
    await expect(page.locator('html')).toHaveAttribute('data-same-document', 'yes')
  })
}

test('a member sees the read-only loss dialog and never requests the manage-only lifecycle', async ({ page }) => {
  // Given
  const api = await prepareLifecycle(page, lostLifecycle, { ...lostAgent, access_level: 'use', creator_id: replacementUser.id })
  await page.goto(settingsPath)
  const dialog = page.getByRole('alertdialog', { name: 'This agent’s workspace stopped running', exact: true })
  await expect(dialog).toContainText('Ask the agent’s manager to start a new container.')
  const update = page.waitForResponse(`**/api/agents/${originalAgent.id}/openclaw-update`)
  // When
  await returnToPage(page)
  await update
  // Then
  await expect(dialog.getByRole('button', { name: 'Close', exact: true })).toBeEnabled()
  await expect(dialog.getByRole('button', { name: 'Start a new container' })).toHaveCount(0)
  expect(api.reads() + api.replacementReads()).toBe(0)
})
