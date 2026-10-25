import { expect, test, type Page } from '@playwright/test'

import type { AgentOut } from '../src/lib/workspace-api'
import { chatPath, prepareChat, submitted } from './chat-identity-fixture'
import { originalAgent, originalToken, originalUser, prepareWorkspace, replacementUser } from './session-fencing-fixture'

const settingsPath = `/app/agents/${originalAgent.id}/settings`
const lifecycleCases = [
  { action: 'start', label: 'Start', initialStatus: 'stopped', finalStatus: 'running' },
  { action: 'stop', label: 'Stop', initialStatus: 'running', finalStatus: 'stopped' },
] as const

async function routeAgent(page: Page, initial: AgentOut = originalAgent) {
  let agent = initial
  let reads = 0
  let metricsReads = 0
  await page.route('**/api/agent-deletion-requests?*', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => {
    expect(route.request().method()).toBe('GET')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    reads++
    return route.fulfill({ json: agent })
  })
  await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => {
    metricsReads++
    return route.fulfill({ json: {
      tokens: { used_today: 123, limit_day: 1000 }, tasks: { done: 2, total: 5 }, approvals: { pending: 3 },
    } })
  })
  return { reads: () => reads, metricsReads: () => metricsReads, setAgent: (next: AgentOut) => { agent = next } }
}

async function deferLifecycle(page: Page, action: 'start' | 'stop') {
  const requested = Promise.withResolvers<void>()
  const response = Promise.withResolvers<number>()
  await page.route(`**/api/agents/${originalAgent.id}/${action}`, async (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    requested.resolve()
    const status = await response.promise
    await route.fulfill({ status, json: status === 200 ? originalAgent : { detail: 'Lifecycle request refused' } })
  })
  return { requested: requested.promise, release: (status = 200) => response.resolve(status) }
}

test('agent information and Back stay in Settings while focused chat and the section rail remain usable', async ({ page }) => {
  const chat = await prepareChat(page)
  const agent = { ...originalAgent, is_expired: true }
  const api = await routeAgent(page, agent)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const sections = page.getByRole('navigation', { name: 'Agent sections' })
  const composer = page.getByRole('textbox', { name: `Message ${agent.name}` })
  await expect(page.getByRole('button', { name: 'Back', exact: true })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: agent.name, exact: true })).toHaveCount(0)
  await expect(page.getByText(agent.role_description, { exact: true })).toHaveCount(0)
  await expect(page.locator('[data-slot="badge"]').filter({ hasText: /^(stopped|manage|Expired)$/ })).toHaveCount(0)
  await expect(page.getByText(/Tokens today/)).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^(Start|Stop)$/ })).toHaveCount(0)
  expect(api.metricsReads()).toBe(0)
  await composer.fill('The chat composer still sends messages')
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect.poll(() => socket.sent.length).toBe(1)
  expect(submitted(socket).content).toBe('The chat composer still sends messages')

  await sections.getByRole('link', { name: 'Settings', exact: true }).click()

  await expect(page).toHaveURL(settingsPath)
  await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: agent.name, exact: true })).toHaveCount(1)
  for (const badge of [agent.status, 'Expired', 'manage']) {
    await expect(page.locator('[data-slot="badge"]').filter({ hasText: new RegExp(`^${badge}$`) })).toHaveCount(1)
  }
  await expect(page.getByText(agent.role_description, { exact: true })).toBeVisible()
  await expect(page.getByText('Tokens today 123 / 1000 · Tasks 2/5 · 3 approvals', { exact: true })).toBeVisible()
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue(agent.name)
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toHaveCount(1)
  expect(api.reads()).toBe(1)
  expect(api.metricsReads()).toBe(1)
  await page.getByRole('button', { name: 'Back', exact: true }).click()
  await expect(page).toHaveURL(chatPath())
  await expect(composer).toBeVisible()
  await expect(sections.getByRole('link', { name: 'Settings', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: agent.name, exact: true })).toHaveCount(0)
})

for (const lifecycle of lifecycleCases) {
  test(`${lifecycle.label} is disabled while pending and refreshes the agent after success`, async ({ page }) => {
    await prepareWorkspace(page)
    const api = await routeAgent(page, { ...originalAgent, status: lifecycle.initialStatus })
    const request = await deferLifecycle(page, lifecycle.action)
    await page.goto(settingsPath)
    const button = page.getByRole('button', { name: lifecycle.label, exact: true })
    await expect(button).toHaveCount(1)

    await button.click()

    await request.requested
    await expect(button).toBeDisabled()
    api.setAgent({ ...originalAgent, status: lifecycle.finalStatus })
    request.release()
    await expect(page.locator('[data-slot="badge"]').filter({ hasText: lifecycle.finalStatus })).toBeVisible()
    expect(api.reads()).toBe(2)
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toHaveCount(lifecycle.action === 'start' ? 0 : 1)
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeEnabled()
    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toHaveCount(0)
  })

  test(`${lifecycle.label} reports failures and re-enables its single control`, async ({ page }) => {
    await prepareWorkspace(page)
    const api = await routeAgent(page, { ...originalAgent, status: lifecycle.initialStatus })
    const request = await deferLifecycle(page, lifecycle.action)
    await page.goto(settingsPath)
    const button = page.getByRole('button', { name: lifecycle.label, exact: true })
    await expect(button).toHaveCount(1)
    await button.click()
    await request.requested
    await expect(button).toBeDisabled()

    request.release(503)

    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toHaveText(`Unable to ${lifecycle.action} this agent`)
    await expect(button).toBeEnabled()
    await expect(page.locator('[data-slot="badge"]').filter({ hasText: lifecycle.initialStatus })).toBeVisible()
    expect(api.reads()).toBe(1)
  })

  for (const status of [200, 503]) {
    test(`late ${lifecycle.action} ${status} cannot affect a signed-out session`, async ({ page }) => {
      await prepareWorkspace(page)
      await routeAgent(page, { ...originalAgent, status: lifecycle.initialStatus })
      const request = await deferLifecycle(page, lifecycle.action)
      await page.goto(settingsPath)
      await page.getByRole('button', { name: lifecycle.label, exact: true }).click()
      await request.requested
      await page.getByRole('button', { name: 'Sign out', exact: true }).first().click()
      await expect(page).toHaveURL('/login')
      await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
      await page.clock.pauseAt(new Date('2030-01-01T01:00:00Z'))
      const completed = page.waitForEvent('requestfinished', {
        predicate: (transport) => transport.method() === 'POST' && new URL(transport.url()).pathname.endsWith(`/${lifecycle.action}`),
      })

      request.release(status)

      await completed
      await page.clock.runFor(1_000)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      await expect(page).toHaveURL('/login')
    })
  }
}

for (const rights of [
  { name: 'creator with use access', creator: true, access: 'use', admin: false, system: false, manage: true },
  { name: 'non-creator manager', creator: false, access: 'manage', admin: false, system: false, manage: true },
  { name: 'use-only member', creator: false, access: 'use', admin: false, system: false, manage: false },
  { name: 'use-only org admin', creator: false, access: 'use', admin: true, system: false, manage: false },
  { name: 'system-agent creator', creator: true, access: 'manage', admin: false, system: true, manage: true },
] as const) {
  test(`Settings preserves lifecycle, form and delete rights for a ${rights.name}`, async ({ page }) => {
    await prepareWorkspace(page)
    await routeAgent(page, {
      ...originalAgent, creator_id: rights.creator ? originalUser.id : replacementUser.id,
      access_level: rights.access, is_system: rights.system,
    })
    await page.route('**/api/member/auth/me', (route) => route.fulfill({ json: {
      ...originalUser, role: rights.admin ? 'org_admin' : 'member',
    } }))
    let setupReads = 0
    await page.route(`**/api/agents/${originalAgent.id}/setup`, (route) => {
      expect(route.request().method()).toBe('GET')
      expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
      setupReads++
      return route.fulfill({ json: { setup_pending: true, state: 'failed', failure_code: null, retryable: true } })
    })

    await page.goto(settingsPath)

    await expect(page.getByLabel('Name', { exact: true })).toBeVisible()
    for (const label of ['Name', 'Bio', 'Welcome message', 'Heartbeat enabled', 'Heartbeat interval (minutes)', 'Active hours', 'Timezone']) {
      await expect(page.getByLabel(label, { exact: true })).toHaveJSProperty('disabled', !rights.creator)
    }
    await expect(page.getByRole('button', { name: 'Start', exact: true })).toHaveCount(rights.manage ? 1 : 0)
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toHaveCount(rights.manage ? 1 : 0)
    await expect(page.getByRole('button', { name: 'Save', exact: true })).toHaveCount(rights.creator || rights.admin ? 1 : 0)
    await expect(page.getByRole('button', { name: 'Request deletion', exact: true })).toHaveCount(rights.creator && !rights.system ? 1 : 0)
    await expect(page.getByRole('button', { name: 'Retry setup', exact: true })).toHaveCount(rights.manage ? 1 : 0)
    if (rights.manage) expect(setupReads).toBeGreaterThanOrEqual(1)
    else expect(setupReads).toBe(0)
  })
}
