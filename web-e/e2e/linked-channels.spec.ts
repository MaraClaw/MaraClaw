import { expect, test } from '@playwright/test'

import { channelApiRoot, channelsPath } from './channels-fixture'
import { prepareLinkedChannels } from './linked-channels-fixture'
import { originalToken, replacementToken, replacementUser, writeFromOtherTab } from './session-fencing-fixture'

test('Telegram saving is write-only configuration with explicit operator webhook registration', async ({ page }) => {
  const fixture = await prepareLinkedChannels(page)
  await page.goto(channelsPath)
  const telegram = page.getByRole('article', { name: 'Telegram bot', exact: true })
  await telegram.getByLabel('Bot token', { exact: true }).fill('synthetic-bot-token')
  await telegram.getByLabel('Webhook secret token', { exact: true }).fill('synthetic-webhook-secret')
  await telegram.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(telegram.getByLabel('Bot token', { exact: true })).toHaveValue('')
  await expect(telegram.getByLabel('Webhook secret token', { exact: true })).toHaveValue('')
  expect(fixture.telegramWrites).toEqual([{ bot_token: 'synthetic-bot-token', secret_token: 'synthetic-webhook-secret' }])
  await expect(telegram).toContainText('Configuration saved; connection not verified.')
  await expect(telegram).toContainText('Saving here does not register the webhook.')
  await expect(telegram.getByLabel('Webhook URL', { exact: true })).toHaveValue('https://synthetic.test/api/telegram/webhook/agent')
  await expect(telegram).toContainText('setWebhook')
})

test('Zalo starts local PNG pairing expires relinks and disconnects with the current generation', async ({ page }) => {
  await page.clock.install()
  const fixture = await prepareLinkedChannels(page)
  await page.goto(channelsPath)
  const zalo = page.getByRole('article', { name: 'Zalo personal', exact: true })
  await zalo.getByRole('button', { name: 'Connect Zalo account' }).click()
  const qr = zalo.getByRole('img', { name: 'Zalo pairing QR' })
  await expect(qr).toHaveAttribute('src', /^data:image\/png;base64,/)
  await page.clock.fastForward(10001)
  await expect(qr).toHaveCount(0)
  await expect(zalo).toContainText('Pairing code expired.')
  await zalo.getByRole('button', { name: 'Relink Zalo account' }).click()
  await expect(qr).toBeVisible()
  fixture.connectZalo()
  await page.clock.fastForward(2001)
  await expect(zalo).toContainText('Zalo agent account connected.')
  await expect(qr).toHaveCount(0)
  await zalo.getByRole('button', { name: 'Disconnect Zalo account' }).click()
  await expect(zalo).toContainText('No Zalo agent account connected.')
  expect(fixture.requests).toContain('POST /zalo-channel/personal/relink')
  expect(fixture.requests).toContain('DELETE /zalo-channel/personal')
})

test('a use-only noncreator links and unlinks both accounts without reading agent credentials', async ({ page }) => {
  await page.clock.install()
  const fixture = await prepareLinkedChannels(page, { creator: false })
  await page.goto(channelsPath)
  for (const [channel, name, code] of [['telegram', 'Telegram bot', 'ABCDE12345'], ['zalo', 'Zalo personal', 'FGHJK67890']] as const) {
    const card = page.getByRole('article', { name, exact: true })
    await card.getByRole('button', { name: 'Issue link code', exact: true }).click()
    await expect(card.locator('code')).toHaveText(`/link ${code}`)
    fixture.linked[channel] = true
    await page.clock.fastForward(2001)
    await expect(card).toContainText('Your account is linked.')
    await expect(card.locator('code')).toHaveCount(0)
    await card.getByRole('button', { name: 'Unlink my account' }).click()
    await expect(card).toContainText('Your account is not linked.')
  }
  expect(fixture.requests.every((request) => request.includes('/channel-links/'))).toBe(true)
  await expect(page.getByLabel('Bot token', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Connect Zalo account' })).toHaveCount(0)
})

test('link codes expire locally and are never written to browser storage', async ({ page }) => {
  await page.clock.install()
  await prepareLinkedChannels(page, { creator: false })
  await page.goto(channelsPath)
  const telegram = page.getByRole('article', { name: 'Telegram bot', exact: true })
  await telegram.getByRole('button', { name: 'Issue link code', exact: true }).click()
  await expect(telegram.locator('code')).toBeVisible()
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('ABCDE12345')
  await page.clock.fastForward(600001)
  await expect(telegram.locator('code')).toHaveCount(0)
  await expect(telegram).toContainText('Link code expired.')
  await expect(telegram.getByRole('link', { name: 'Open private chat' })).toHaveCount(0)
})

test('a retired member code response cannot disclose a code in the replacement session', async ({ page }) => {
  await prepareLinkedChannels(page, { creator: false })
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**${channelApiRoot}/channel-links/telegram/code`, async (route) => {
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    requested.resolve()
    await release.promise
    await route.fulfill({ status: 201, json: { channel: 'telegram', code: 'ABCDE67890', expires_at: new Date(Date.now() + 600000).toISOString(), deep_link: null, account_hint: 'Old owner account' } })
  })
  try {
    await page.goto(channelsPath)
    const telegram = page.getByRole('article', { name: 'Telegram bot', exact: true })
    await telegram.getByRole('button', { name: 'Issue link code', exact: true }).click()
    await requested.promise
    await writeFromOtherTab(page, replacementToken)
    await expect(page.getByText(replacementUser.email, { exact: true }).first()).toBeVisible()
    release.resolve()
    await expect(telegram.getByRole('button', { name: 'Issue link code', exact: true })).toBeEnabled()
    await expect(telegram.locator('code')).toHaveCount(0)
    await expect(page.getByText('Old owner account', { exact: true })).toHaveCount(0)
  } finally { release.resolve() }
})

for (const width of [390, 1440, 1920]) for (const theme of ['light', 'dark']) {
  test(`linked channels reflow at ${width} in ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 1000 })
    await prepareLinkedChannels(page, { qr: true })
    await page.addInitScript((theme) => localStorage.setItem('maraclaw-theme', theme), theme)
    await page.goto(channelsPath)
    const telegram = page.getByRole('article', { name: 'Telegram bot', exact: true })
    const zalo = page.getByRole('article', { name: 'Zalo personal', exact: true })
    await telegram.getByRole('button', { name: 'Issue link code', exact: true }).click()
    await expect(zalo.getByRole('img', { name: 'Zalo pairing QR' })).toBeVisible()
    await page.evaluate(() => scrollTo(0, 0))
    await page.screenshot({ path: info.outputPath(`channels-${width}-${theme}-after.png`), fullPage: true })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}
