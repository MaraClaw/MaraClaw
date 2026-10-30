import { expect, test, type Request } from '@playwright/test'

import { channelApiRoot, channelCatalog, channelsPath, googleConfig, prepareChannels } from './channels-fixture'
import { originalToken, replacementToken, replacementUser, writeFromOtherTab } from './session-fencing-fixture'

test('catalog owns all channel names and capabilities without unsupported setup actions', async ({ page }) => {
  const catalog = channelCatalog.map((channel) => channel.channel_type === 'slack'
    ? { ...channel, display_name: 'Engine-owned Slack name', supports_inbound: false, supports_proactive: false }
    : channel)
  const fixture = await prepareChannels(page, { catalog })
  await page.goto(channelsPath)
  for (const channel of catalog) await expect(page.getByRole('article', { name: channel.display_name, exact: true })).toBeVisible()
  for (const name of ['Atlassian', 'AgentBay', 'Engine-owned Slack name']) {
    await expect(page.getByRole('article', { name, exact: true }).getByRole('button')).toHaveCount(0)
  }
  await expect(page.getByRole('article', { name: 'Discord', exact: true })).toContainText('Proactive messages: not supported')
  expect(fixture.reads.some((path) => /atlassian|agentbay|slack/.test(path))).toBe(false)
  expect(fixture.reads.some((path) => path.endsWith('/teams-channel'))).toBe(true)
})

test('catalog failure exposes retry and never installs local setup cards', async ({ page }) => {
  await prepareChannels(page)
  let available = false
  await page.route('**/api/channel-catalog', (route) => route.fulfill(!available
    ? { status: 503, json: { detail: 'Synthetic unavailable' } }
    : { json: channelCatalog }))
  await page.goto(channelsPath)
  await expect(page.getByRole('button', { name: 'Retry catalog' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save', exact: true })).toHaveCount(0)
  available = true
  await page.getByRole('button', { name: 'Retry catalog' }).click()
  await expect(page.getByRole('article', { name: 'AgentBay', exact: true })).toBeVisible()
})

test('Google Chat saves unrelated fields by omission and accepts only fresh credential replacements', async ({ page }, testInfo) => {
  const fixture = await prepareChannels(page)
  await page.goto(channelsPath)
  const google = page.getByRole('article', { name: 'Google Chat', exact: true })
  await expect(google.getByLabel('Project number', { exact: true })).toHaveValue('123456')
  await expect(google.getByLabel('Service account JSON', { exact: true })).toHaveValue('')
  await expect(google.getByLabel('Client email', { exact: true })).toHaveValue('')
  await expect(google).toContainText('Service account credentials configured.')
  await google.getByLabel('Audience', { exact: true }).fill('replacement-audience')
  await google.getByRole('button', { name: 'Save', exact: true }).click()
  await expect.poll(() => fixture.writes.length).toBe(1)
  expect(fixture.writes[0]).toEqual({ project_number: '123456', audience: 'replacement-audience' })
  await expect(google.getByRole('button', { name: 'Save', exact: true })).toBeEnabled()
  const fresh = JSON.stringify({ client_email: 'synthetic@provider.test', private_key: 'synthetic-replacement' })
  await google.getByLabel('Service account JSON', { exact: true }).fill(fresh)
  await google.getByRole('button', { name: 'Save', exact: true }).click()
  await expect.poll(() => fixture.writes.length).toBe(2)
  expect(fixture.writes[1]).toEqual({ project_number: '123456', service_account_json: fresh })
  await expect(google.getByLabel('Service account JSON', { exact: true })).toHaveValue('')
  await google.getByLabel('Project number', { exact: true }).fill('654321')
  await google.getByRole('button', { name: 'Save', exact: true }).click()
  await expect.poll(() => fixture.writes.length).toBe(3)
  expect(fixture.writes[2]).toEqual({ project_number: '654321' })
  await expect(google.getByRole('button', { name: 'Save', exact: true })).toBeEnabled()
  await google.screenshot({ path: testInfo.outputPath('google-chat-after.png') })
})

test('Google Chat shows inbound-only when saved without a service account', async ({ page }, testInfo) => {
  // Given: an existing audience-only configuration, with no service-account metadata.
  await prepareChannels(page, { serviceAccount: false })
  // When: the creator opens channel setup.
  await page.goto(channelsPath)
  // Then: the saved configuration remains inbound-only with empty write-only inputs.
  const google = page.getByRole('article', { name: 'Google Chat', exact: true })
  await expect(google).toContainText('Configuration saved; connection not verified.')
  await expect(google).toContainText('No service account credentials configured. Inbound-only setup is available.')
  await expect(google.getByLabel('Service account JSON', { exact: true })).toHaveValue('')
  await expect(google.getByLabel('Client email', { exact: true })).toHaveValue('')
  await google.screenshot({ path: testInfo.outputPath('google-chat-no-service-account.png') })
})

test('Google Chat retains inbound-only creation without any credentials', async ({ page }) => {
  const fixture = await prepareChannels(page, { configured: false, serviceAccount: false })
  await page.goto(channelsPath)
  const google = page.getByRole('article', { name: 'Google Chat', exact: true })
  await expect(google).toContainText('No service account credentials configured. Inbound-only setup is available.')
  await google.getByLabel('Project number', { exact: true }).fill('123456')
  await google.getByRole('button', { name: 'Save', exact: true }).click()
  await expect.poll(() => fixture.writes.length).toBe(1)
  expect(fixture.writes[0]).toEqual({ project_number: '123456' })
  await expect(google).toContainText('No service account credentials configured. Inbound-only setup is available.')
})

test('manage access does not reveal creator-only credentials or QR controls', async ({ page }) => {
  const fixture = await prepareChannels(page, { creator: false })
  await page.goto(channelsPath)
  await expect(page.getByRole('article', { name: 'Google Chat', exact: true })).toBeVisible()
  await expect(page.getByRole('textbox')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Save|Request login QR|Remove/ })).toHaveCount(0)
  expect(fixture.reads).toEqual([])
})

test('WeChat keeps its separate creator-only QR workflow', async ({ page }) => {
  const fixture = await prepareChannels(page)
  await page.goto(channelsPath)
  await page.getByRole('button', { name: 'Request login QR' }).click()
  await expect(page.getByRole('img', { name: 'WeChat login QR' })).toBeVisible()
  expect(fixture.reads).toContain(`POST ${channelApiRoot}/wechat-channel/qrcode`)
})

test('a late Google Chat save cannot restore drafts or toast after session replacement', async ({ page }) => {
  await prepareChannels(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**${channelApiRoot}/google-chat-channel`, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    requested.resolve()
    await release.promise
    await route.fulfill({ status: 201, json: googleConfig })
  })
  try {
    await page.goto(channelsPath)
    const google = page.getByRole('article', { name: 'Google Chat', exact: true })
    await google.getByLabel('Service account JSON', { exact: true }).fill('{"private_key":"synthetic-old-owner","client_email":"synthetic@provider.test"}')
    await google.getByRole('button', { name: 'Save', exact: true }).click()
    await requested.promise
    await writeFromOtherTab(page, replacementToken)
    await expect(page.getByText(replacementUser.email, { exact: true }).first()).toBeVisible()
    const completion = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/google-chat-channel'))
    release.resolve()
    await (await completion).finished()
    await expect(google.getByLabel('Service account JSON', { exact: true })).toHaveValue('')
    await expect(page.getByText('Google Chat saved', { exact: true })).toHaveCount(0)
  } finally { release.resolve() }
})

test('a late original catalog cannot replace the new session catalog', async ({ page }) => {
  await prepareChannels(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const retired = new Map<Request, Promise<void>>()
  await page.route('**/api/channel-catalog', async (route) => {
    if (route.request().headers().authorization === `Bearer ${replacementToken}`) return route.fulfill({ json: [{ ...channelCatalog[0], display_name: 'Replacement catalog' }] })
    const request = route.request()
    retired.set(request, new Promise<void>((resolve) => {
      const settle = (completed: Request) => {
        if (completed !== request) return
        page.off('requestfailed', settle)
        page.off('requestfinished', settle)
        resolve()
      }
      page.on('requestfailed', settle)
      page.on('requestfinished', settle)
    }))
    requested.resolve()
    await release.promise
    await route.fulfill({ json: [{ ...channelCatalog[0], display_name: 'Retired catalog' }] })
  })
  try {
    await page.goto(channelsPath)
    await requested.promise
    await writeFromOtherTab(page, replacementToken)
    await expect(page.getByRole('heading', { name: 'Replacement catalog' })).toBeVisible()
    release.resolve()
    await Promise.all(retired.values())
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())))
    await expect(page.getByRole('heading', { name: 'Replacement catalog' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Retired catalog' })).toHaveCount(0)
  } finally { release.resolve() }
})

for (const width of [390, 1440]) {
  for (const theme of ['light', 'dark']) {
    test(`channel setup reflows at ${width} in ${theme}`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 })
      await prepareChannels(page)
      await page.addInitScript((theme) => localStorage.setItem('maraclaw-theme', theme), theme)
      await page.goto(channelsPath)
      const google = page.getByRole('article', { name: 'Google Chat', exact: true })
      await expect(google.getByLabel('Service account JSON', { exact: true })).toBeVisible()
      await google.scrollIntoViewIfNeeded()
      await page.screenshot({ path: testInfo.outputPath(`channels-${width}-${theme}-after.png`) })
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    })
  }
}
