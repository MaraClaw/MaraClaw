import { expect, test } from '@playwright/test'
import { accept, chatPath, firstMessage, message, prepareChat, submitted } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'

for (const width of [390, 1440, 1920]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme} submission states`, () => {
      test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, isMobile: width === 390,
        hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme, timezoneId: 'America/New_York' })

      test('receipt states stay distinct and retry remains keyboard operable', async ({ page }, testInfo) => {
        const chat = await prepareChat(page)
        const errors: string[] = []
        page.on('pageerror', (error) => errors.push(error.message))
        await page.clock.install({ time: new Date('2030-01-01T05:00:00Z') })
        await page.goto(chatPath())
        const socket = await chat.socket()
        await page.clock.pauseAt(new Date('2030-01-01T05:07:00Z'))
        const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
        await composer.fill('Keep one logical submission')
        await composer.press('Enter')
        await expect.poll(() => socket.sent.length).toBe(1)
        const envelope = submitted(socket)
        const row = page.locator(`[data-chat-entry="${envelope.client_submission_id}"]`)
        await expect(row.locator('time')).toHaveText('00:07')
        await expect(row.locator('time')).toHaveAttribute('datetime', '2030-01-01T05:07:00.000Z')
        await expect(row.getByRole('img', { name: 'Accepted', exact: true })).toHaveCount(0)
        socket.route.send(JSON.stringify({ type: 'turn_accepted', client_submission_id: envelope.client_submission_id }))
        await expect(page.getByRole('button', { name: 'Retry this message', exact: true })).toBeVisible()
        await expect(row.getByRole('img', { name: 'Accepted', exact: true })).toHaveCount(0)
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
        await expect(row.getByRole('img', { name: 'Accepted', exact: true })).toHaveCount(0)
        await expect(row.locator('time')).toHaveText('00:07')
        await page.screenshot({ path: testInfo.outputPath('rejected.png'), fullPage: true })
        await retry.click()
        await expect.poll(() => socket.sent.length).toBe(3)
        accept(socket, envelope.client_submission_id)
        await expect(retry).toHaveCount(0)
        await expect(row.getByRole('img', { name: 'Accepted', exact: true })).toBeVisible()
        await expect(page.getByText('Queue temporarily unavailable.', { exact: false })).toHaveCount(0)
        await page.clock.fastForward(90_000)
        await expect(row.locator('time')).toHaveText('00:07')
        await expect(row.locator('time')).toHaveAttribute('datetime', '2030-01-01T05:07:00.000Z')
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

      test('history uses canonical local times and leaves missing metadata absent', async ({ page }) => {
        // Given: genuine history metadata, including legacy rows without dates or message identity.
        const chat = await prepareChat(page, [
          { ...message('history-user', 'Saved question', 'user'), created_at: '2030-07-01T16:09:00Z' },
          { ...message('history-agent', 'Saved reply'), created_at: '2030-01-01T05:03:00Z' },
          { ...message('history-undated', 'Undated reply'), created_at: null },
          { ...message('history-invalid', 'Invalid date reply'), created_at: 'not-a-date' },
          { role: 'user', entry_id: 'legacy-user', content: 'Unidentified legacy question' },
        ])
        // When
        await page.goto(chatPath())
        const socket = await chat.socket()
        // Then: summer/winter offsets and midnight use the browser's local 24-hour clock.
        const user = page.locator('[data-chat-entry="history-user"]')
        await expect(user.locator('time')).toHaveText('12:09')
        await expect(user.locator('time')).toHaveAttribute('datetime', '2030-07-01T16:09:00Z')
        await expect(user.getByRole('img', { name: 'Accepted', exact: true })).toBeVisible()
        await expect(page.locator('[data-chat-entry="history-agent"] time')).toHaveText('00:03')
        await expect(page.locator('[data-chat-entry="history-agent"]').getByRole('img', { name: 'Accepted' })).toHaveCount(0)
        await expect(page.locator('[data-chat-entry="history-undated"] time')).toHaveCount(0)
        await expect(page.locator('[data-chat-entry="history-invalid"] time')).toHaveCount(0)
        await expect(page.locator('[data-chat-entry="legacy-user"] [data-chat-metadata]')).toHaveCount(0)
        socket.route.send(JSON.stringify({ type: 'chunk', content: 'Undated streaming reply' }))
        await expect(page.locator('article[aria-live="polite"]')).toHaveText('Undated streaming reply')
        await expect(page.locator('article[aria-live="polite"] time')).toHaveCount(0)
        socket.route.send(JSON.stringify({ type: 'done', message_id: firstMessage, content: 'Undated streaming reply' }))
        await expect(page.locator(`[data-chat-entry="${firstMessage}"]`)).toBeVisible()
        await expect(page.locator(`[data-chat-entry="${firstMessage}"] time`)).toHaveCount(0)
      })

      test('upload appearance time survives receipt and canonical history replaces it', async ({ page }) => {
        const chat = await prepareChat(page)
        await page.route('**/api/chat/upload', (route) => route.fulfill({ json: {
          filename: 'notes.txt', extracted_text: 'Notes', workspace_path: 'uploads/notes.txt',
        } }))
        await page.clock.install({ time: new Date('2030-01-01T05:00:00Z') })
        await page.goto(chatPath())
        const socket = await chat.socket()
        await page.clock.pauseAt(new Date('2030-01-01T05:07:00Z'))
        await page.getByLabel('Attach a file', { exact: true }).setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('Notes') })
        await expect.poll(() => socket.sent.length).toBe(1)
        const envelope = submitted(socket)
        const row = page.locator(`[data-chat-entry="${envelope.client_submission_id}"]`)
        await expect(row.locator('time')).toHaveAttribute('datetime', '2030-01-01T05:07:00.000Z')
        await expect(row.getByRole('img', { name: 'Accepted', exact: true })).toHaveCount(0)
        await page.clock.fastForward(60_000)
        accept(socket, envelope.client_submission_id)
        await expect(row.getByRole('img', { name: 'Accepted', exact: true })).toBeVisible()
        await expect(row.locator('time')).toHaveText('00:07')
        chat.setHistory([{ ...message(firstMessage, 'notes.txt', 'user'), client_submission_id: envelope.client_submission_id,
          attachments: [{ path: 'uploads/notes.txt', name: 'notes.txt' }], created_at: '2030-01-01T05:08:00Z' }])
        await page.clock.resume()
        socket.route.send(JSON.stringify({ type: 'done', message_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', content: 'Received attachment' }))
        const canonical = page.locator(`[data-chat-entry="${firstMessage}"]`)
        await expect(canonical.locator('time')).toHaveText('00:08')
        await expect(canonical.locator('time')).toHaveAttribute('datetime', '2030-01-01T05:08:00Z')
        await expect(canonical.getByRole('img', { name: 'Accepted', exact: true })).toBeVisible()
        await expect(canonical.getByRole('button', { name: 'Preview', exact: true })).toBeVisible()
        await expect(canonical.getByRole('button', { name: 'Download', exact: true })).toBeVisible()
      })
    })
  }
}
