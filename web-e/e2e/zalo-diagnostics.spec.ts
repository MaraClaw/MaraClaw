import { expect, test } from '@playwright/test'

import { channelApiRoot, channelsPath } from './channels-fixture'
import { generation, prepareLinkedChannels } from './linked-channels-fixture'
import { originalToken, replacementToken, replacementUser, writeFromOtherTab } from './session-fencing-fixture'
import { diagnosticCases } from './zalo-diagnostics-fixture'

for (const width of [390, 1440]) for (const theme of ['light', 'dark'] as const) {
  for (const [name, status] of Object.entries(diagnosticCases)) {
    test(`Zalo ${name} diagnostics and recovery at ${width} in ${theme}`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 1000 })
      await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
      const fixture = await prepareLinkedChannels(page, { status })
      await page.addInitScript((theme) => localStorage.setItem('maraclaw-theme', theme), theme)
      await page.goto(channelsPath)
      const card = page.getByRole('region', { name: 'Zalo personal connection', exact: true })
      const list = card.locator('dl')
      const value = (label: string) => list.locator('div').filter({ has: page.locator('dt', { hasText: label }) }).locator('dd')
      await expect(value('Recovery attempts')).toHaveText(String(status.diagnostics.reconnect_attempts))
      await expect(value('Heartbeat failures')).toHaveText(String(status.diagnostics.heartbeat_failures))
      await expect(value('Last heartbeat').locator('time')).toHaveAttribute('datetime', status.diagnostics.heartbeat_at)
      await expect(value('Last activity').locator('time')).toHaveAttribute('datetime', status.diagnostics.last_activity_at)
      await expect(value('Connected since').locator('time')).toHaveAttribute('datetime', status.diagnostics.connected_at)
      await expect(value('Next retry').locator('time')).toHaveCount(status.diagnostics.retry_at ? 1 : 0)
      if (status.diagnostics.retry_at) await expect(value('Next retry').locator('time')).toHaveAttribute('datetime', status.diagnostics.retry_at)
      await expect(value('Last problem')).toHaveCount(status.diagnostics.last_error ? 1 : 0)
      if (status.diagnostics.last_error) {
        await expect(value('Last problem')).not.toContainText(status.diagnostics.last_error)
        await expect(value('Last problem').locator('time')).toHaveAttribute('datetime', status.diagnostics.last_error_at)
      }
      await expect(card.getByRole('img')).toHaveCount(0)
      const resume = card.getByRole('button', { name: 'Reconnect Zalo account', exact: true })
      const resumable = status.state === 'paused' || status.state === 'failed'
      await expect(resume).toHaveCount(resumable ? 1 : 0)
      await card.scrollIntoViewIfNeeded()
      await page.screenshot({ path: info.outputPath(`${name}-${width}-${theme}.png`) })
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      if (resumable) {
        await resume.focus()
        await expect(resume).toBeFocused()
        await page.keyboard.press('Enter')
        await expect(resume).toHaveCount(0)
        await expect(value('Heartbeat failures')).toHaveText('0')
        await expect(value('Last problem')).toHaveCount(0)
        await expect(card.getByRole('img')).toHaveCount(0)
        expect(fixture.requests.filter((request) => request.startsWith('POST /zalo'))).toEqual(['POST /zalo-channel/personal/start'])
      }
    })
  }
}

for (const status of [
  { ...diagnosticCases.exhausted, resumable: false },
  { ...diagnosticCases.exhausted, generation: null },
]) {
  test(`Zalo resume requires saved credentials and generation: ${status.resumable}/${status.generation}`, async ({ page }) => {
    const fixture = await prepareLinkedChannels(page, { status })
    await page.goto(channelsPath)
    const card = page.getByRole('region', { name: 'Zalo personal connection', exact: true })
    await expect(card.locator('dl')).toBeVisible()
    await expect(card.getByRole('button', { name: 'Reconnect Zalo account', exact: true })).toHaveCount(0)
    expect(fixture.requests.some((request) => request.startsWith('POST /zalo'))).toBe(false)
  })
}

test('Zalo diagnostics and resume stay creator-only while member linking remains available', async ({ page }) => {
  const fixture = await prepareLinkedChannels(page, { creator: false, status: diagnosticCases.exhausted })
  await page.goto(channelsPath)
  const card = page.getByRole('article', { name: 'Zalo personal', exact: true })
  await expect(card.getByRole('button', { name: 'Issue link code', exact: true })).toBeEnabled()
  await expect(card.locator('dl')).toHaveCount(0)
  await expect(card.getByRole('button', { name: 'Reconnect Zalo account', exact: true })).toHaveCount(0)
  expect(fixture.requests.some((request) => request.includes('/zalo-channel/'))).toBe(false)
})

test('Zalo explicit relink replaces the saved login through its own endpoint', async ({ page }) => {
  const fixture = await prepareLinkedChannels(page, { status: diagnosticCases.exhausted })
  await page.goto(channelsPath)
  const card = page.getByRole('region', { name: 'Zalo personal connection', exact: true })
  await card.getByRole('button', { name: 'Relink Zalo account', exact: true }).click()
  await expect(card.getByRole('img', { name: 'Zalo pairing QR', exact: true })).toBeVisible()
  expect(fixture.requests.filter((request) => request.startsWith('POST /zalo'))).toEqual(['POST /zalo-channel/personal/relink'])
})

test('a late Zalo resume response cannot enter the replacement owner view', async ({ page }) => {
  await prepareLinkedChannels(page, { status: diagnosticCases.exhausted })
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**${channelApiRoot}/zalo-channel/personal/start`, async (route) => {
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    expect(route.request().postDataJSON()).toEqual({ expected_generation: generation })
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { ...diagnosticCases.connected, revision: 20 } })
  })
  try {
    await page.goto(channelsPath)
    await page.getByRole('button', { name: 'Reconnect Zalo account', exact: true }).click()
    await requested.promise
    await writeFromOtherTab(page, replacementToken)
    await expect(page.getByText(replacementUser.email, { exact: true }).first()).toBeVisible()
    release.resolve()
    await expect(page.getByRole('region', { name: 'Zalo personal connection', exact: true })).toHaveCount(0)
    await expect(page.getByRole('article', { name: 'Zalo personal', exact: true }).getByRole('button', { name: 'Issue link code', exact: true })).toBeEnabled()
  } finally { release.resolve() }
})
