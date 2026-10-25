import { expect, test, type Page } from '@playwright/test'

import type { AgentSetup } from '../src/lib/agent-setup-api'
import { originalAgent, originalToken, prepareWorkspace, replacementCompany, replacementToken, writeFromOtherTab } from './session-fencing-fixture'

const settingsPath = `/app/agents/${originalAgent.id}/settings`
const setupPath = `**/api/agents/${originalAgent.id}/setup`
const failed: AgentSetup = { setup_pending: true, state: 'failed', failure_code: 'private-internal-code', retryable: true }
const pending: AgentSetup = { setup_pending: true, state: 'pending', failure_code: null, retryable: false }
const succeeded: AgentSetup = { setup_pending: false, state: 'succeeded', failure_code: null, retryable: false }

async function prepareSetup(page: Page, initial: AgentSetup) {
  await prepareWorkspace(page)
  let setup = initial
  let agentReads = 0
  let setupReads = 0
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => {
    expect(route.request().method()).toBe('GET')
    if (route.request().headers().authorization === `Bearer ${replacementToken}`) {
      return route.fulfill({ status: 403, json: { detail: 'Original company agent is not available to the replacement company' } })
    }
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    agentReads++
    return route.fulfill({ json: originalAgent })
  })
  await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route('**/api/agent-deletion-requests?*', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.route(setupPath, (route) => {
    expect(route.request().method()).toBe('GET')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    setupReads++
    return route.fulfill({ json: setup })
  })
  return { set: (next: AgentSetup) => { setup = next }, agentReads: () => agentReads, setupReads: () => setupReads }
}

async function deferRetry(page: Page) {
  const requested = Promise.withResolvers<void>()
  const response = Promise.withResolvers<number>()
  let requests = 0
  await page.route(`${setupPath}/retry`, async (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    expect(route.request().postData()).toBeNull()
    requests++
    requested.resolve()
    const status = await response.promise
    await route.fulfill({ status, json: status < 300 ? pending : { detail: 'private-retry-stack-trace' } })
  })
  return { requested: requested.promise, requests: () => requests, release: (status = 202) => response.resolve(status) }
}

for (const state of ['pending', 'running'] as const) {
  test(`${state} setup blocks Start and polling restores it after completion`, async ({ page }) => {
    // Given
    const api = await prepareSetup(page, { ...pending, state })
    await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
    await page.goto(settingsPath)
    await expect(page.getByRole('region', { name: 'Agent setup' })).toContainText(state === 'pending' ? 'Setup pending' : 'Setup in progress')
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
    // When
    api.set(succeeded)
    await page.clock.runFor(1600)
    // Then
    await expect(page.getByRole('region', { name: 'Agent setup' }).getByRole('status')).toContainText('Setup complete')
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeEnabled()
    expect(api.agentReads()).toBe(2)
    const reads = api.setupReads()
    await page.clock.runFor(3200)
    expect(api.setupReads()).toBe(reads)
  })
}

test('unknown setup status blocks Start and offers an adjacent safe status retry', async ({ page }) => {
  // Given
  await prepareSetup(page, succeeded)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  let status = 503
  await page.route(setupPath, async (route) => {
    requested.resolve()
    await release.promise
    await route.fulfill({ status, json: status === 200 ? succeeded : { detail: 'private-status-stack-trace' } })
  })
  try {
    await page.goto(settingsPath)
    await requested.promise
    await expect(page.getByText('Checking setup status', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
    release.resolve()
    await expect(page.getByRole('alert')).toContainText('Unable to check setup status')
    // When
    status = 200
    await page.getByRole('button', { name: 'Retry status check', exact: true }).click()
    // Then
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeEnabled()
    await expect(page.getByText(/private-status-stack-trace/)).toHaveCount(0)
  } finally { release.resolve() }
})

test('authorized double-click retry sends one request and installs pending status before polling completes setup', async ({ page }) => {
  // Given
  const api = await prepareSetup(page, failed)
  const retry = await deferRetry(page)
  await page.goto(settingsPath)
  await expect(page.getByText('Setup failed', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
  try {
    // When
    await page.getByRole('button', { name: 'Retry setup', exact: true }).click({ clickCount: 2 })
    await retry.requested
    // Then
    await expect(page.getByRole('button', { name: 'Retrying setup…', exact: true })).toBeDisabled()
    api.set(pending)
    retry.release()
    await expect(page.getByText('Setup pending', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Retry setup', exact: true })).toHaveCount(0)
    api.set(succeeded)
    await expect(page.getByText('Setup complete', { exact: true })).toBeVisible()
    expect(retry.requests()).toBe(1)
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeEnabled()
    await expect(page.getByText(/private-internal-code/)).toHaveCount(0)
  } finally { retry.release() }
})

for (const status of [409, 503]) {
  test(`retry ${status} keeps failed setup visible and reports a safe adjacent error`, async ({ page }) => {
    // Given
    await prepareSetup(page, failed)
    const retry = await deferRetry(page)
    await page.goto(settingsPath)
    try {
      // When
      await page.getByRole('button', { name: 'Retry setup', exact: true }).click()
      await retry.requested
      retry.release(status)
      // Then
      await expect(page.getByRole('alert')).toContainText('Unable to retry setup')
      await expect(page.getByText('Setup failed', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Retry setup', exact: true })).toBeEnabled()
      await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
      await expect(page.getByText(/private-retry-stack-trace|private-internal-code/)).toHaveCount(0)
    } finally { retry.release() }
  })
}

test('non-retryable failure offers company-admin help without an unauthorized action', async ({ page }) => {
  // Given
  await prepareSetup(page, { ...failed, retryable: false })
  // When
  await page.goto(settingsPath)
  // Then
  await expect(page.getByRole('region', { name: 'Agent setup' }).getByRole('status')).toContainText('Contact your company admin')
  await expect(page.getByRole('button', { name: 'Retry setup', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled()
})

for (const status of [202, 503]) {
  for (const token of [null, replacementToken]) {
    test(`late retry ${status} stays fenced after cross-tab ${token === null ? 'sign-out' : 'replacement'}`, async ({ page }) => {
      // Given
      await prepareSetup(page, failed)
      const retry = await deferRetry(page)
      await page.goto(settingsPath)
      await page.getByRole('button', { name: 'Retry setup', exact: true }).click()
      await retry.requested
      try {
        // When
        await writeFromOtherTab(page, token)
        await expect(page.getByRole('region', { name: 'Agent setup' })).toHaveCount(0)
        if (token !== null) {
          await expect(page.getByText(replacementCompany.name, { exact: true })).toHaveCount(1)
          await expect(page.getByText('Original company agent is not available to the replacement company', { exact: true })).toBeVisible()
        }
        const completed = page.waitForEvent('requestfinished', { predicate: (request) => request.method() === 'POST' && request.url().endsWith('/setup/retry') })
        retry.release(status)
        await completed
        // Then
        await expect(page.getByText(/Unable to retry setup|Setup pending|Setup failed/)).toHaveCount(0)
        await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      } finally { retry.release() }
    })
  }
}
