import { expect, test } from '@playwright/test'
import { chatPath, firstSession, message, prepareChat } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'

test('busy polling still recovers a persisted reply without a completion frame', async ({ page }) => {
  // Given
  const old = message('old', 'Already loaded reply')
  const question = message('poll-user', 'Question awaiting reply', 'user')
  const reply = message('poll-reply', 'Recovered by busy polling')
  const chat = await prepareChat(page, [old])
  await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
  await page.goto(chatPath())
  const socket = await chat.socket()
  await page.clock.pauseAt(new Date('2030-01-01T01:00:00Z'))
  await page.getByPlaceholder(`Message ${originalAgent.name}…`).fill(question.content)
  await page.getByPlaceholder(`Message ${originalAgent.name}…`).press('Enter')
  await expect.poll(() => socket.sent.length).toBe(1)
  await expect(page.locator('[aria-busy]')).toHaveAttribute('aria-busy', 'true')
  chat.setHistory([question, reply])
  const refreshed = page.waitForEvent('requestfinished', {
    predicate: (request) => new URL(request.url()).pathname.endsWith(`/sessions/${firstSession}/messages`),
  })
  // When
  await page.clock.runFor(5_000)
  await refreshed
  await page.clock.runFor(100)
  // Then
  await expect(page.locator('article')).toHaveText([old.content, question.content, reply.content])
  await expect(page.locator('[aria-busy]')).toHaveAttribute('aria-busy', 'false')
})
