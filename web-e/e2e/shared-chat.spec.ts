import { expect, test } from '@playwright/test'
import { archivedSession, chatPath, complete, firstSession, message, navigateChat, prepareChat } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'

test('opening chat ensures the empty shared conversation instead of the newest private primary', async ({ page }) => {
  const chat = await prepareChat(page)
  await page.goto(`/app/agents/${originalAgent.id}/chat`)
  const socket = await chat.socket()
  expect(socket.session).toBe(firstSession)
  expect(chat.ensures()).toBe(1)
  await expect(page.locator('article')).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: `Message ${originalAgent.name}` })).toBeVisible()
  await expect(page.getByText('Everyone with access to this agent shares this conversation, including messages from connected channels.')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /New (session|chat)/ })).toHaveCount(0)
  await expect(page.getByRole('main').getByRole('complementary')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^(Shared conversation|Archived private thread|Edit|Del)$/ })).toHaveCount(0)
})

test('authorized archive links stay readable and read-only with a return to shared', async ({ page }) => {
  const chat = await prepareChat(page, [message('shared-reply', 'Shared current history')])
  await page.goto(chatPath(archivedSession))
  await expect(page.getByText('Authorized archived history', { exact: true })).toBeVisible()
  await expect(page.getByRole('textbox', { name: `Message ${originalAgent.name}` })).toHaveCount(0)
  await expect(page.getByLabel('Attach a file', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0)
  expect(chat.sockets).toEqual([])
  await page.getByRole('link', { name: 'Return to shared conversation', exact: true }).click()
  expect((await chat.socket()).session).toBe(firstSession)
  await expect(page.locator('article')).toHaveText(['Shared current history'])
})

test('an unauthorized archive response never falls back to shared history or enables sending', async ({ page }) => {
  const chat = await prepareChat(page, [message('secret-shared', 'Not the requested archive')])
  await page.route(`**/sessions/${archivedSession}/messages/page?*`, (route) => route.fulfill({ status: 403, json: { detail: 'Archive access denied' } }))
  await page.goto(chatPath(archivedSession))
  await expect(page.getByText('Archive access denied', { exact: true })).toBeVisible()
  await expect(page.locator('article')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0)
  expect(chat.sockets).toEqual([])
  expect(chat.ensures()).toBe(0)
})

test('switching from a live shared conversation to a denied archive clears the retired view', async ({ page }) => {
  const chat = await prepareChat(page, [message('shared-before-archive', 'Only in the shared conversation')])
  await page.route(`**/sessions/${archivedSession}/messages/page?*`, (route) => route.fulfill({ status: 403, json: { detail: 'Archive access denied' } }))
  await page.goto(chatPath())
  const socket = await chat.socket()
  socket.route.send(JSON.stringify({ type: 'agentbay_live', env: 'browser', live_preview: { screenshot_url: '/preview.png' } }))
  socket.route.send(JSON.stringify({ type: 'chunk', content: 'In-flight shared reply' }))
  await expect(page.getByRole('link', { name: 'Take Control', exact: true })).toBeVisible()
  await expect(page.getByText('In-flight shared reply', { exact: true })).toBeVisible()
  await navigateChat(page, chatPath(archivedSession))
  await expect(page.getByText('Archive access denied', { exact: true })).toBeVisible()
  complete(socket.route, message('late-shared-reply', 'Retired shared completion'))
  socket.route.send(JSON.stringify({ type: 'agentbay_live', env: 'browser', live_preview: { screenshot_url: '/retired-preview.png' } }))
  await expect(page.locator('article')).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Take Control', exact: true })).toHaveCount(0)
  await expect(page.getByText('Thinking', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0)
  expect(chat.sockets).toHaveLength(1)
})

test('an archive ending in a human turn does not pretend to await a new reply', async ({ page }) => {
  await prepareChat(page)
  await page.route(`**/sessions/${archivedSession}/messages/page?*`, (route) => route.fulfill({ json: {
    items: [message('archived-user', 'Last archived question', 'user')], next_cursor: null, has_more: false,
  } }))
  await page.goto(chatPath(archivedSession))
  await expect(page.locator('article')).toHaveText(['Last archived question'])
  await expect(page.getByText('Thinking', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Accepted. Waiting for a reply.', { exact: true })).toHaveCount(0)
})

test('idle shared history refresh picks up another human and IM turns without a local send', async ({ page }) => {
  const chat = await prepareChat(page)
  await page.clock.install()
  await page.goto(chatPath())
  const socket = await chat.socket()
  chat.setHistory([message('other-human', 'Another member asked', 'user'), message('im-reply', 'Reply from a connected channel')])
  await page.clock.runFor(5000)
  await expect(page.locator('article')).toHaveText(['Another member asked', 'Reply from a connected channel'])
  expect(socket.sent).toEqual([])
  complete(socket.route, message('im-reply', 'Reply from a connected channel'))
  await expect(page.locator('article')).toHaveCount(2)
})
