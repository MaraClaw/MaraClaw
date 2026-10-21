import { expect, test, type Page } from '@playwright/test'

import { originalAgent, prepareWorkspace } from './session-fencing-fixture'

const controlPath = `/api/agents/${originalAgent.id}/control/`
const ownerToken = 'fixture-control-owner'
const preview = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI4MDAiIGhlaWdodD0iNDAwIj48cmVjdCB3aWR0aD0iODAwIiBoZWlnaHQ9IjQwMCIgZmlsbD0iI2Q3ZTNlOCIvPjwvc3ZnPg=='

async function prepareControl(page: Page) {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route(`**/api/agents/${originalAgent.id}/sessions?**`, (route) => route.fulfill({
    json: [{ id: 'control-session', title: 'Control thread', source_channel: 'shared' }],
  }))
  await page.clock.install()
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  return errors
}

for (const loss of [
  { endpoint: 'screenshot', status: 200 },
  { endpoint: 'screenshot', status: 400 },
  { endpoint: 'press_keys', status: 409 },
  { endpoint: 'unlock', status: 409 },
  { endpoint: 'current-url', status: 400 },
] as const) {
  test(`ends control when ${loss.endpoint} reports lease loss (${loss.status})`, async ({ page }) => {
    // Given
    const errors = await prepareControl(page)
    const requests: string[] = []
    let loseLease = false
    await page.route(`**${controlPath}*`, async (route) => {
      const endpoint = new URL(route.request().url()).pathname.slice(controlPath.length)
      requests.push(endpoint)
      if (loseLease && endpoint === loss.endpoint) {
        await route.fulfill({ status: loss.status, json: { status: 'lost_lock', detail: 'Lease expired' } })
        return
      }
      await route.fulfill({ json: {
        status: endpoint === 'lock' ? 'locked' : 'ok',
        owner_token: ownerToken,
        screenshot: preview, url: 'https://vendor.example.test/login',
      } })
    })
    await page.goto(`/app/agents/${originalAgent.id}/control`)
    if (loss.endpoint !== 'current-url') {
      await page.getByRole('button', { name: 'Take control', exact: true }).click()
      await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
      await expect(page.getByRole('button', { name: 'Release control' })).toBeEnabled()
      await page.getByPlaceholder('Type into the remote session').fill('sensitive pending input')
    }
    // When
    loseLease = true
    switch (loss.endpoint) {
      case 'screenshot':
        await page.clock.runFor(2500)
        break
      case 'press_keys':
        await page.getByRole('button', { name: 'Enter', exact: true }).click()
        break
      case 'unlock':
        await page.getByRole('button', { name: 'Release control' }).click()
        break
      case 'current-url':
        await page.getByRole('button', { name: 'Take control', exact: true }).click()
        break
      default:
        loss.endpoint satisfies never
    }
    // Then
    await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
    await expect(page.getByAltText('Live AgentBay session')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Enter', exact: true })).toBeDisabled()
    await expect(page.getByPlaceholder('Type into the remote session')).toBeDisabled()
    await expect(page.getByPlaceholder('Type into the remote session')).toHaveValue('')
    await expect(page.getByLabel('Cookie domain on unlock')).toHaveValue('')
    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toHaveCount(1)
    const requestCount = requests.length
    await page.clock.runFor(10000)
    expect(requests).toHaveLength(requestCount)
    expect(errors).toEqual([])
  })
}

test('normal control forwards input and releases with cookie export', async ({ page }) => {
  // Given
  const errors = await prepareControl(page)
  const requests: { endpoint: string; body: unknown }[] = []
  await page.route(`**${controlPath}*`, async (route) => {
    const endpoint = new URL(route.request().url()).pathname.slice(controlPath.length)
    requests.push({ endpoint, body: route.request().postDataJSON() })
    await route.fulfill({ json: {
      status: endpoint === 'lock' ? 'locked' : 'ok', screenshot: preview,
      owner_token: ownerToken,
      url: 'https://vendor.example.test/login', cookies_exported: endpoint === 'unlock', cookie_count: 2,
    } })
  })
  await page.goto(`/app/agents/${originalAgent.id}/control`)
  // When
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
  await page.getByPlaceholder('Type into the remote session').fill('vendor login')
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect(page.getByPlaceholder('Type into the remote session')).toHaveValue('')
  await page.getByRole('button', { name: 'Enter', exact: true }).click()
  await page.getByAltText('Live AgentBay session').click({ position: { x: 100, y: 100 } })
  await page.getByRole('button', { name: 'Release control' }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
  expect(requests.find(({ endpoint }) => endpoint === 'type')?.body).toMatchObject({ text: 'vendor login' })
  expect(requests.find(({ endpoint }) => endpoint === 'press_keys')?.body).toMatchObject({ keys: ['enter'] })
  expect(requests.some(({ endpoint }) => endpoint === 'click')).toBe(true)
  for (const request of requests.filter(({ endpoint }) => endpoint !== 'lock')) {
    expect(request.body).toMatchObject({ session_id: 'control-session', owner_token: ownerToken })
  }
  expect(requests.find(({ endpoint }) => endpoint === 'unlock')?.body).toMatchObject({
    export_cookies: true, platform_hint: 'vendor.example.test',
  })
  await expect(page.getByAltText('Live AgentBay session')).toHaveCount(0)
  const count = requests.length
  await page.clock.runFor(10000)
  expect(requests).toHaveLength(count)
  expect(errors).toEqual([])
})

test('late screenshot and repeated loss cannot revive or notify a retired lease', async ({ page }) => {
  // Given
  const errors = await prepareControl(page)
  const screenshotRequested = Promise.withResolvers<void>()
  const screenshotReply = Promise.withResolvers<void>()
  const actionRequested = Promise.withResolvers<void>()
  const actionReply = Promise.withResolvers<void>()
  let holdScreenshot = false
  let actions = 0
  let screenshots = 0
  await page.route(`**${controlPath}*`, async (route) => {
    const endpoint = new URL(route.request().url()).pathname.slice(controlPath.length)
    if (endpoint === 'press_keys') {
      if (++actions === 1) { actionRequested.resolve(); await actionReply.promise }
      await route.fulfill({ json: { status: 'lost_lock' } })
      return
    }
    if (endpoint === 'screenshot') {
      screenshots++
      if (holdScreenshot) { screenshotRequested.resolve(); await screenshotReply.promise }
    }
    await route.fulfill({ json: { status: endpoint === 'lock' ? 'locked' : 'ok', owner_token: ownerToken, screenshot: preview } })
  })
  await page.goto(`/app/agents/${originalAgent.id}/control`)
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
  holdScreenshot = true
  await page.clock.runFor(2500)
  await screenshotRequested.promise
  await page.getByRole('button', { name: 'Enter', exact: true }).click()
  await actionRequested.promise
  // When
  await page.getByRole('button', { name: 'Tab', exact: true }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
  const lateReplies = [
    page.waitForEvent('requestfinished', (request) => request.url().endsWith('/screenshot')),
    page.waitForEvent('requestfinished', (request) => request.url().endsWith('/press_keys')),
  ]
  screenshotReply.resolve()
  actionReply.resolve()
  await Promise.all(lateReplies)
  await expect(page.locator('[data-sonner-toast][data-type="error"]')).toHaveCount(1)
  await page.clock.runFor(10000)
  await expect(page.getByAltText('Live AgentBay session')).toHaveCount(0)
  expect(screenshots).toBe(2)
  expect(errors).toEqual([])
})

test('a fresh acquisition survives late errors from its previous owner token', async ({ page }) => {
  // Given
  const errors = await prepareControl(page)
  const requested = Promise.withResolvers<void>()
  const reply = Promise.withResolvers<void>()
  const bodies: unknown[] = []
  let acquisitions = 0
  let actions = 0
  await page.route(`**${controlPath}*`, async (route) => {
    const endpoint = new URL(route.request().url()).pathname.slice(controlPath.length)
    if (endpoint === 'lock') {
      await route.fulfill({ json: { status: 'locked', owner_token: `owner-${++acquisitions}` } })
      return
    }
    bodies.push(route.request().postDataJSON())
    if (endpoint === 'press_keys') {
      if (++actions === 1) { requested.resolve(); await reply.promise }
      await route.fulfill({ status: 409, json: { detail: { code: 'take_control_lost' } } })
      return
    }
    await route.fulfill({ json: { status: 'ok', screenshot: preview } })
  })
  await page.goto(`/app/agents/${originalAgent.id}/control`)
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
  await page.getByRole('button', { name: 'Enter', exact: true }).click()
  await requested.promise
  await page.getByRole('button', { name: 'Tab', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
  // When
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
  const finished = page.waitForEvent('requestfinished', (request) => request.url().endsWith('/press_keys'))
  reply.resolve()
  await finished
  await page.clock.runFor(2500)
  // Then
  await expect(page.getByRole('button', { name: 'Release control' })).toBeEnabled()
  await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
  expect(bodies.at(-1)).toMatchObject({ owner_token: 'owner-2' })
  expect(page.url()).not.toContain('owner-')
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('owner-')
  await expect(page.locator('[data-sonner-toast][data-type="error"]')).toHaveCount(1)
  expect(errors).toEqual([])
})

test('a transient screenshot failure is handled without abandoning valid control', async ({ page }) => {
  // Given
  const errors = await prepareControl(page)
  let unavailable = false
  await page.route(`**${controlPath}*`, async (route) => {
    const endpoint = new URL(route.request().url()).pathname.slice(controlPath.length)
    if (unavailable && endpoint === 'screenshot') {
      await route.fulfill({ status: 503, json: { detail: { code: 'take_control_unavailable' } } })
      return
    }
    await route.fulfill({ json: { status: endpoint === 'lock' ? 'locked' : 'ok', owner_token: ownerToken, screenshot: preview } })
  })
  await page.goto(`/app/agents/${originalAgent.id}/control`)
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
  // When
  unavailable = true
  await page.clock.runFor(2500)
  // Then
  await expect(page.locator('[data-sonner-toast][data-type="error"]')).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Release control' })).toBeEnabled()
  expect(errors).toEqual([])
})
