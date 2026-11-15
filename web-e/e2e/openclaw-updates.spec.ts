import { expect, test, type Page } from '@playwright/test'

import { NotificationKind } from '../src/lib/notifications-api'
import type { AgentUpdate } from '../src/lib/openclaw-updates-api'
import { lifecycleReady } from './agent-lifecycle-data'
import { originalAgent, originalToken, prepareWorkspace, replacementUser } from './session-fencing-fixture'

const agentId = originalAgent.id
const notice = {
  id: '11111111-1111-4111-8111-111111111111', title: 'OpenClaw workspace upgrade',
  body: 'This update improves the agent runtime.\nSave your work, open Settings, and wait for the upgrade to finish.',
  mandatory: true, created_at: '2026-10-22T09:00:00Z',
}
const runId = '33333333-3333-4333-8333-333333333333'
const agentPath = `/app/agents/${agentId}`

async function prepare(page: Page) {
  await prepareWorkspace(page)
  const update: AgentUpdate = { notice, run: null, required: true, can_upgrade: true }
  const state = {
    agent: { ...originalAgent, name: 'Research assistant' },
    update,
    upgrades: 0, starts: 0, fileReads: 0, statusError: false,
    statusReady: Promise.resolve(),
  }
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    const url = new URL(request.url())
    const path = url.pathname
    expect(request.method()).toBe(path.endsWith('/start') || (path.endsWith('/openclaw-update') && request.method() === 'POST') ? 'POST' : 'GET')
    switch (path) {
      case '/api/agent-deletion-requests': return route.fulfill({ json: { items: [], next_cursor: null } })
      case '/api/notifications/unread-count': return route.fulfill({ json: { unread_count: 1 } })
      case '/api/notifications/page':
        expect(Object.fromEntries(url.searchParams)).toEqual({ category: 'all', limit: '50', view: 'active', unread_only: 'false' })
        return route.fulfill({ json: { items: [{ ...notice, type: NotificationKind.OpenClawUpdate, is_read: false, archived_at: null, link: `/app/updates/${notice.id}` }], next_cursor: null } })
      case '/api/agents/page': return route.fulfill({ json: { items: [state.agent], next_cursor: null, has_more: false } })
      case `/api/agents/${agentId}`: return route.fulfill({ json: state.agent })
      case `/api/agents/${agentId}/metrics`: return route.fulfill({ json: {} })
      case `/api/agents/${agentId}/lifecycle`: return route.fulfill({ json: { ...lifecycleReady, runtime: { ...lifecycleReady.runtime, recorded_status: state.agent.status }, upgrade: state.update } })
      case `/api/agents/${agentId}/start`:
        state.starts += 1
        state.agent.status = 'running'
        return route.fulfill({ json: state.agent })
      case `/api/agents/${agentId}/files/page`:
        state.fileReads += 1
        return route.fulfill({ json: { items: [], next_cursor: null } })
      case `/api/openclaw-updates/${notice.id}`: return route.fulfill({ json: { notice: state.update.notice, agents: [{ agent_id: agentId, ...state.agent, update: state.update }] } })
      case `/api/agents/${agentId}/openclaw-update`:
        if (route.request().method() === 'POST') {
          expect(route.request().postDataJSON()).toEqual({ notice_id: notice.id })
          state.upgrades += 1
          state.update = { ...state.update, run: { id: runId, state: 'pending', failure_code: null } }
          return route.fulfill({ status: 202, json: state.update.run })
        }
        await state.statusReady
        return state.statusError
          ? route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
          : route.fulfill({ json: state.update })
      default: return route.fallback()
    }
  })
  return state
}

for (const width of [390, 1440, 1920]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width}, ${colorScheme}`, () => {
      test.use({ viewport: { width, height: 900 }, isMobile: width === 390, hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme, reducedMotion: 'reduce' })
      test('notification detail groups content in a card below the back link', async ({ page }, testInfo) => {
        // Given
        await prepare(page)
        await page.goto('/app/notifications')
        // When
        await page.getByRole('link', { name: `Open notification: ${notice.title}` }).click()
        // Then
        const back = page.getByRole('link', { name: 'Back to Inbox', exact: true })
        await expect(back).toBeVisible()
        await expect(back.locator('svg.lucide-move-left')).toBeVisible()
        await expect(back.locator('svg.lucide-move-left')).toHaveAttribute('aria-hidden', 'true')
        const detail = page.locator('[data-slot="card"]').filter({ has: page.getByRole('heading', { name: notice.title, exact: true }) })
        await expect(detail).toBeVisible()
        await expect(detail).toHaveCSS('border-top-width', '1px')
        await expect(detail).toHaveCSS('border-top-style', 'solid')
        await expect(detail.getByText(notice.body)).toBeVisible()
        await expect(detail.getByRole('region', { name: 'Agents for this update' })).toBeVisible()
        await expect(detail.getByRole('link', { name: 'Back to Inbox', exact: true })).toHaveCount(0)
        expect(await back.evaluate((element) => element.getBoundingClientRect().bottom)).toBeLessThan(await detail.evaluate((element) => element.getBoundingClientRect().top))
        await page.screenshot({ path: testInfo.outputPath('notice-detail.png') })
        await back.focus()
        await expect(back).toBeFocused()
        await back.press('Enter')
        await expect(page).toHaveURL('/app/notifications')
        await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).toBeVisible()
      })
      test('mandatory upgrade stays gated through failure and releases on completion', async ({ page }, testInfo) => {
      // Given
      const state = await prepare(page)
      await page.goto('/app/notifications')
      // When
      await page.getByRole('link', { name: `Open notification: ${notice.title}` }).click()
      // Then
      await expect(page.getByRole('heading', { name: notice.title })).toBeVisible()
      await expect(page.getByText(notice.body)).toBeVisible()
      expect(state.starts).toBe(0)
      expect(state.upgrades).toBe(0)
      await page.screenshot({ path: testInfo.outputPath('notice.png') })
      await page.getByRole('link', { name: 'Upgrade in Settings', exact: true }).click()
      const upgrade = page.getByRole('button', { name: 'Upgrade', exact: true })
      await expect(upgrade).toBeDisabled()
      await page.screenshot({ path: testInfo.outputPath('stopped-settings.png') })
      await page.getByRole('button', { name: 'Start', exact: true }).click()
      await expect(upgrade).toBeEnabled()
      await page.screenshot({ path: testInfo.outputPath('ready-settings.png') })
      await upgrade.click()
      await expect(page.getByRole('button', { name: 'Upgrading…', exact: true })).toBeDisabled()
      await page.getByRole('link', { name: 'Files', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Agent work is paused' })).toBeVisible()
      expect(state.fileReads).toBe(0)
      await page.screenshot({ path: testInfo.outputPath('pending-gate.png') })
      state.update = { ...state.update, run: { id: runId, state: 'cutover', failure_code: null } }
      await expect(page.getByRole('status').filter({ hasText: 'Upgrade in progress.' })).toBeVisible()
      await page.screenshot({ path: testInfo.outputPath('cutover-gate.png') })
      state.update = { ...state.update, run: { id: runId, state: 'failed', failure_code: 'sandbox_unavailable' } }
      await expect(page.getByRole('status').filter({ hasText: 'Upgrade failed.' })).toBeVisible()
      expect(state.fileReads).toBe(0)
      await page.screenshot({ path: testInfo.outputPath('failed-gate.png') })
      await page.getByRole('link', { name: 'Open agent Settings' }).click()
      const retry = page.getByRole('button', { name: 'Retry Upgrade', exact: true })
      await expect(retry).toBeEnabled()
      await page.screenshot({ path: testInfo.outputPath('retry-settings.png') })
      await retry.click()
      await expect(page.getByRole('button', { name: 'Upgrading…', exact: true })).toBeDisabled()
      state.update = { ...state.update, required: false, run: { id: runId, state: 'succeeded', failure_code: null } }
      await expect(page.getByRole('button', { name: 'Upgraded', exact: true })).toBeDisabled()
      await page.screenshot({ path: testInfo.outputPath('completed-settings.png') })
      await page.getByLabel('Active hours', { exact: true }).scrollIntoViewIfNeeded()
      await page.screenshot({ path: testInfo.outputPath('settings-end.png') })
      await page.getByRole('link', { name: 'Files', exact: true }).click()
      await expect(page.getByRole('button', { name: 'Root', exact: true })).toBeVisible()
      expect(state.fileReads).toBe(1)
      expect(state.upgrades).toBe(2)
      expect(state.starts).toBe(1)
      })
    })
  }
}

for (const tab of ['files', 'chat']) {
test(`initial loading and status errors never mount ${tab}, while Settings stays reachable`, async ({ page }, testInfo) => {
  // Given
  const state = await prepare(page)
  const statusReady = Promise.withResolvers<void>()
  state.statusReady = statusReady.promise
  // When
  await page.goto(`${agentPath}/${tab}`)
  // Then
  await expect(page.getByRole('heading', { name: 'Agent work is paused' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Back', exact: true })).toHaveCount(0)
  await expect(page.getByRole('navigation', { name: 'Agent sections', exact: true })).toBeVisible()
  expect(state.fileReads).toBe(0)
  await page.screenshot({ path: testInfo.outputPath('loading-gate.png') })
  state.statusError = true
  statusReady.resolve()
  await expect(page.locator('[data-sonner-toast][data-type="error"]').filter({ hasText: 'Unable to confirm OpenClaw upgrade status' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Back', exact: true })).toHaveCount(0)
  expect(state.fileReads).toBe(0)
  await page.screenshot({ path: testInfo.outputPath('error-gate.png') })
  await page.getByRole('link', { name: 'Open agent Settings' }).click()
  await expect(page.getByRole('heading', { name: state.agent.name })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Retry status check' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Back', exact: true })).toHaveCount(0)
})
}

test('optional use-only agents allow work before an upgrade but cannot start one', async ({ page }, testInfo) => {
  // Given
  const state = await prepare(page)
  state.agent.status = 'running'
  state.agent.access_level = 'use'
  state.agent.creator_id = replacementUser.id
  state.update = { ...state.update, notice: { ...notice, mandatory: false }, required: false, can_upgrade: false }
  // When
  await page.goto(`${agentPath}/files`)
  // Then
  await expect(page.getByRole('button', { name: 'Root', exact: true })).toBeVisible()
  await page.locator(`a[href="${agentPath}/settings"]`).click()
  await expect(page.getByText('Contact an agent manager if you cannot upgrade this agent.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Upgrade', exact: true })).toBeDisabled()
  await page.screenshot({ path: testInfo.outputPath('use-only-settings.png') })
  expect(state.upgrades).toBe(0)
})
