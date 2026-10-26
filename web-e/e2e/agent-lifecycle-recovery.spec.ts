import { expect, test } from '@playwright/test'

import { lifecycleReady, warningLifecycle } from './agent-lifecycle-data'
import { deferMCPRetry, lifecyclePath, prepareLifecycle, settingsPath } from './agent-lifecycle-fixture'
import { originalAgent } from './session-fencing-fixture'

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
      return [document.querySelector('input#agent-name'), [...document.querySelectorAll('button')].find((item) => item.textContent === 'Start')]
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
