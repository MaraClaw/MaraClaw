import { expect, test } from '@playwright/test'
import { accept, chatPath, prepareChat, submitted } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'

for (const width of [390, 1440, 1920]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme} submission states`, () => {
      test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, isMobile: width === 390,
        hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme })

      test('receipt states stay distinct and retry remains keyboard operable', async ({ page }, testInfo) => {
        const chat = await prepareChat(page)
        const errors: string[] = []
        page.on('pageerror', (error) => errors.push(error.message))
        await page.clock.install()
        await page.goto(chatPath())
        const socket = await chat.socket()
        const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
        await composer.fill('Keep one logical submission')
        await composer.press('Enter')
        await expect.poll(() => socket.sent.length).toBe(1)
        const envelope = submitted(socket)
        socket.route.send(JSON.stringify({ type: 'turn_accepted', client_submission_id: envelope.client_submission_id }))
        await expect(page.getByRole('button', { name: 'Retry this message', exact: true })).toBeVisible()
        await page.screenshot({ path: testInfo.outputPath('unknown.png'), fullPage: true })
        const retry = page.getByRole('button', { name: 'Retry this message', exact: true })
        await page.keyboard.press('Shift+Tab')
        await expect(retry).toBeFocused()
        expect(await retry.evaluate((button) => button.matches(':focus-visible'))).toBe(true)
        await page.screenshot({ path: testInfo.outputPath('retry-focus.png'), fullPage: true, animations: 'disabled' })
        await retry.press('Enter')
        await expect.poll(() => socket.sent.length).toBe(2)
        expect(socket.sent[1]).toBe(socket.sent[0])
        socket.route.send(JSON.stringify({ type: 'turn_rejected', client_submission_id: envelope.client_submission_id,
          reason: 'enqueue_failed', content: 'Queue temporarily unavailable.' }))
        await expect(retry).toBeVisible()
        await page.screenshot({ path: testInfo.outputPath('rejected.png'), fullPage: true })
        await retry.click()
        await expect.poll(() => socket.sent.length).toBe(3)
        accept(socket, envelope.client_submission_id)
        await expect(retry).toHaveCount(0)
        await expect(page.getByText('Queue temporarily unavailable.', { exact: false })).toHaveCount(0)
        await page.clock.fastForward(90_000)
        await expect(page.getByText('Accepted. The reply is taking longer than usual. No need to send again.', { exact: true })).toBeVisible()
        await page.screenshot({ path: testInfo.outputPath('accepted-slow.png'), fullPage: true })
        await page.emulateMedia({ reducedMotion: 'reduce' })
        await page.screenshot({ path: testInfo.outputPath('accepted-slow-reduced-motion.png'), fullPage: true })
        await composer.fill('A different draft, not a duplicate send')
        await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled()
        expect(socket.sent.every((data) => data === socket.sent[0])).toBe(true)
        expect(errors).toEqual([])
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
      })
    })
  }
}
