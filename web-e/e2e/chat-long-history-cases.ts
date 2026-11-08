import { expect, test } from '@playwright/test'
import { chatPath, message, prepareChat, submitted } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'

export function registerLongHistoryTests() {
  test('long history keeps a bounded DOM, holds the measured anchor, evicts pages near latest and reenters its floor', async ({ page }, testInfo) => {
    const history = Array.from({ length: 1200 }, (_, index) => ({
      ...message(`long-${index}`, `History ${index}. ${'A reply with enough text to wrap on a phone. '.repeat(index % 4 + 1)}`),
      created_at: new Date(Date.UTC(2029, 0, 1) + index * 1000).toISOString(),
    }))
    const chat = await prepareChat(page, history)
    const cursors: (string | null)[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (url.pathname.endsWith('/messages/page')) cursors.push(url.searchParams.get('cursor'))
    })
    await page.goto(chatPath())
    await chat.socket()
    const thread = page.locator('main [aria-busy]')
    await expect(page.locator('[data-chat-entry="long-1199"]')).toBeInViewport()
    await thread.evaluate((element) => { element.scrollTop = 120 })
    await expect.poll(chat.requests).toBe(2)
    for (let count = 150; count <= 1200; count += 50) {
      const earlier = page.getByRole('button', { name: 'Load earlier messages', exact: true })
      await expect(earlier).toBeEnabled()
      await earlier.evaluate((element) => { if (element instanceof HTMLElement) element.focus({ preventScroll: true }) })
      await earlier.press('Enter')
      await expect.poll(chat.requests).toBe(count / 50)
    }
    await expect(page.getByText('Beginning of conversation', { exact: true })).toHaveCount(1)
    expect(await page.locator('[data-chat-entry]').count()).toBeLessThan(40)
    const anchor = await thread.evaluate((element) => {
      const top = element.getBoundingClientRect().top
      const row = [...element.querySelectorAll<HTMLElement>('[data-chat-entry]')].find((entry) => entry.getBoundingClientRect().bottom > top)
      if (!row) throw new Error('Expected a reading anchor')
      return { id: row.dataset.chatEntry, offset: row.getBoundingClientRect().top - top }
    })
    await page.setViewportSize({ width: 390, height: 844 })
    await expect.poll(async () => page.locator(`[data-chat-entry="${anchor.id}"]`).evaluate((element) => {
      const thread = element.closest('[aria-busy]')
      return element.getBoundingClientRect().top - (thread?.getBoundingClientRect().top ?? 0)
    })).toBeCloseTo(anchor.offset, 0)
    await page.screenshot({ path: testInfo.outputPath('long-history-mobile.png') })
    await thread.evaluate((element) => { element.scrollTop = element.scrollHeight })
    await expect(page.locator('[data-chat-entry="long-1199"]')).toBeInViewport()
    await expect(page.getByRole('button', { name: 'Load earlier messages', exact: true })).toBeEnabled()
    const release = chat.holdNextHistory()
    await thread.evaluate((element) => { element.scrollTop = 120 })
    await expect.poll(() => cursors.at(-1)).toBe('long-700')
    await release()
    await expect(page.getByRole('button', { name: 'Load earlier messages', exact: true })).toBeEnabled()
    expect(await page.locator('[data-chat-entry]').count()).toBeLessThan(40)
    await page.screenshot({ path: testInfo.outputPath('long-history-reentered.png') })
  })

  test('pending and rejected retry rows remain mounted outside the virtual viewport and keep focus', async ({ page }) => {
    const chat = await prepareChat(page, Array.from({ length: 50 }, (_, index) => message(`pinned-${index}`, `Older row ${index}`)))
    await page.goto(chatPath())
    const socket = await chat.socket()
    const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
    await composer.fill('Keep this retry mounted')
    await composer.press('Enter')
    await expect.poll(() => socket.sent.length).toBe(1)
    const submission = submitted(socket)
    const pending = page.locator(`[data-chat-entry="${submission.client_submission_id}"]`)
    await page.locator('main [aria-busy]').evaluate((element) => { element.scrollTop = 300 })
    await expect(pending).toHaveCount(1)
    socket.route.send(JSON.stringify({ type: 'turn_rejected', reason: 'enqueue_failed', content: 'Try again.', client_submission_id: submission.client_submission_id }))
    const retry = pending.getByRole('button', { name: 'Retry this message' })
    await expect(retry).toBeEnabled()
    await retry.evaluate((element) => { if (element instanceof HTMLElement) element.focus({ preventScroll: true }) })
    await page.locator('main [aria-busy]').evaluate((element) => { element.scrollTop = 500 })
    await expect(retry).toBeFocused()
    await retry.press('Enter')
    await expect.poll(() => socket.sent.length).toBe(2)
    expect(submitted(socket, 1)).toEqual(submission)
  })
}
