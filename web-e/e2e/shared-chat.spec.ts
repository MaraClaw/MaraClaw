import { expect, test, type Page } from '@playwright/test'
import { LANGUAGE_STORAGE_KEY } from '../src/lib/i18n/translation'
import { chatPath, complete, firstSession, message, navigateChat, peerMember, prepareChat, readOnlySession, sessionRow } from './chat-identity-fixture'
import { originalAgent, originalUser } from './session-fencing-fixture'
import { prepareVideoChat, videoAttachment, videoMessage } from './attachment-video-fixture'

const ownPath = `/app/agents/${originalAgent.id}/chat`
const copy = {
  en: { conversations: 'Other conversations', own: 'Your conversation', readOnly: 'Read-only', back: 'Return to your conversation' },
  vi: { conversations: 'Các cuộc trò chuyện khác', own: 'Cuộc trò chuyện của bạn', readOnly: 'Chỉ đọc', back: 'Quay lại cuộc trò chuyện của bạn' },
} as const

async function useLocale(page: Page, locale: keyof typeof copy) {
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [LANGUAGE_STORAGE_KEY, locale] as const)
}

async function openConversations(page: Page, name: string = copy.en.conversations) {
  const conversations = page.getByRole('navigation', { name })
  await conversations.getByRole('button', { name, exact: true }).click()
  return conversations
}

const returnLink = (page: Page) => page.getByRole('link', { name: copy.en.back, exact: true })

test('live IM video references remain lazy and survive authoritative history reload', async ({ page }) => {
  const fixture = await prepareVideoChat(page)
  fixture.chat.setHistory([])
  await page.goto(chatPath())
  const socket = await fixture.chat.socket()
  complete(socket.route, videoMessage)
  await expect(page.locator('article')).toHaveCount(1)
  await expect(page.getByText(videoAttachment.name, { exact: true })).toBeVisible()
  expect(fixture.metadataCalls()).toBe(0)
  expect(fixture.byteCalls()).toBe(0)
  fixture.chat.setHistory([videoMessage])
  await page.reload()
  await expect(page.locator('article')).toHaveCount(1)
  await expect(page.getByText(videoAttachment.name, { exact: true })).toBeVisible()
  expect(fixture.metadataCalls()).toBe(0)
  expect(fixture.byteCalls()).toBe(0)
  await page.getByRole('button', { name: 'Preview', exact: true }).click()
  await expect(page.locator('video')).toBeVisible()
  expect(fixture.metadataCalls()).toBe(1)
  expect(fixture.byteCalls()).toBe(1)
})

for (const locale of ['en', 'vi'] as const) {
  test(`a member who created the agent opens their own conversation and lists only their own conversations (${locale})`, async ({ page }, testInfo) => {
    // Given the agent's creator, whose company role is member, in the chosen language
    expect(originalUser.role).toBe('member')
    expect(originalAgent.creator_id).toBe(originalUser.id)
    const chat = await prepareChat(page)
    await useLocale(page, locale)
    // When
    await page.goto(ownPath)
    // Then their own conversation is ensured once and opened for writing
    expect((await chat.socket()).session).toBe(firstSession)
    expect(chat.ensures()).toBe(1)
    await expect(page.locator('article')).toHaveCount(0)
    await expect(page.getByRole('main').getByRole('textbox')).toBeVisible()
    // And it is listed as theirs, next to the unchanged peer-agent session, without a colleague's conversation
    const conversations = await openConversations(page, copy[locale].conversations)
    const own = conversations.getByRole('link', { name: new RegExp(`^${copy[locale].own}`) })
    await expect(own).toHaveAttribute('href', ownPath)
    await expect(own).toHaveAttribute('aria-current', 'page')
    await expect(conversations.getByRole('link', { name: /^Identity thread 2/ })).toBeVisible()
    await expect(conversations.getByRole('link')).toHaveCount(2)
    await expect(conversations.getByText(copy[locale].readOnly, { exact: true })).toHaveCount(0)
    await expect(page.getByText(/shared conversation|trò chuyện (được )?chia sẻ|trò chuyện chung/i)).toHaveCount(0)
    // And as a member they asked only for their own conversations, so no access error appears
    expect(chat.sessionRequests).toEqual([{ scope: 'mine', limit: '50', cursor: null }])
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath(`member-own-conversation-${locale}.png`), fullPage: true })
  })

  test(`an org admin reads a colleague's conversation by name, read-only and without a composer (${locale})`, async ({ page }, testInfo) => {
    // Given a company admin with their own history, in the chosen language
    const chat = await prepareChat(page, [message('own-reply', 'Your own history')], { role: 'org_admin' })
    await useLocale(page, locale)
    await page.goto(ownPath)
    expect((await chat.socket()).session).toBe(firstSession)
    // When they list every conversation
    const conversations = await openConversations(page, copy[locale].conversations)
    // Then the colleague's conversation carries the owner's name and a read-only badge; their own and the peer-agent session do not
    const colleague = conversations.getByRole('link', { name: new RegExp(`^${peerMember.username}`) })
    await expect(colleague.getByText(copy[locale].readOnly, { exact: true })).toBeVisible()
    await expect(conversations.getByRole('link', { name: new RegExp(`^${copy[locale].own}`) }).getByText(copy[locale].readOnly)).toHaveCount(0)
    await expect(conversations.getByRole('link', { name: /^Identity thread 2/ }).getByText(copy[locale].readOnly)).toHaveCount(0)
    expect(chat.sessionRequests).toEqual([{ scope: 'all', limit: '50', cursor: null }])
    await page.screenshot({ path: testInfo.outputPath(`org-admin-conversations-${locale}.png`), fullPage: true })
    // When they open it
    await colleague.click()
    // Then its history is readable with no composer and no socket
    await expect(page).toHaveURL(chatPath(readOnlySession))
    await expect(page.getByText('Peer member history', { exact: true })).toBeVisible()
    await expect(page.getByRole('main').getByRole('textbox')).toHaveCount(0)
    await expect(page.getByRole('main').locator('form')).toHaveCount(0)
    expect(chat.sockets).toHaveLength(1)
    // And the banner returns them to their own conversation
    await page.getByRole('link', { name: copy[locale].back, exact: true }).click()
    await expect(page).toHaveURL(ownPath)
    expect((await chat.socket(1)).session).toBe(firstSession)
    await expect(page.getByText('Your own history', { exact: true })).toBeVisible()
  })
}

test('an unauthorized read-only response never falls back to your history or enables sending', async ({ page }) => {
  const chat = await prepareChat(page, [message('own-secret', 'Not the requested conversation')])
  await page.route(`**/sessions/${readOnlySession}/messages/page?*`, (route) => route.fulfill({ status: 403, json: { detail: 'Conversation access denied' } }))
  await page.goto(chatPath(readOnlySession))
  await expect(page.getByText('Conversation access denied', { exact: true })).toBeVisible()
  await expect(page.locator('article')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0)
  expect(chat.sockets).toEqual([])
  expect(chat.ensures()).toBe(0)
})

test('switching from your live conversation to a denied one clears the retired view', async ({ page }) => {
  const chat = await prepareChat(page, [message('own-before-switch', 'Only in your conversation')])
  await page.route(`**/sessions/${readOnlySession}/messages/page?*`, (route) => route.fulfill({ status: 403, json: { detail: 'Conversation access denied' } }))
  await page.goto(chatPath())
  const socket = await chat.socket()
  socket.route.send(JSON.stringify({ type: 'agentbay_live', env: 'browser', live_preview: { screenshot_url: '/preview.png' } }))
  socket.route.send(JSON.stringify({ type: 'chunk', content: 'In-flight reply' }))
  await expect(page.getByRole('link', { name: 'Take Control', exact: true })).toBeVisible()
  await expect(page.getByText('In-flight reply', { exact: true })).toBeVisible()
  await navigateChat(page, chatPath(readOnlySession))
  await expect(page.getByText('Conversation access denied', { exact: true })).toBeVisible()
  complete(socket.route, message('late-own-reply', 'Retired completion'))
  socket.route.send(JSON.stringify({ type: 'agentbay_live', env: 'browser', live_preview: { screenshot_url: '/retired-preview.png' } }))
  await expect(page.locator('article')).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Take Control', exact: true })).toHaveCount(0)
  await expect(page.getByText('Thinking', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0)
  expect(chat.sockets).toHaveLength(1)
})

test('a read-only conversation ending in a human turn does not pretend to await a new reply', async ({ page }) => {
  await prepareChat(page, [], { role: 'org_admin' })
  await page.route(`**/sessions/${readOnlySession}/messages/page?*`, (route) => route.fulfill({ json: {
    items: [message('colleague-user', 'Last colleague question', 'user')], next_cursor: null, has_more: false, revision: 'read-only-1',
  } }))
  await page.goto(chatPath(readOnlySession))
  await expect(page.locator('article > [data-chat-content]')).toHaveText(['Last colleague question'])
  await expect(page.getByText('Thinking', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Accepted. Waiting for a reply.', { exact: true })).toHaveCount(0)
})

test('idle refresh of your own conversation picks up connected-channel turns without a local send', async ({ page }) => {
  const chat = await prepareChat(page)
  await page.clock.install()
  await page.goto(chatPath())
  const socket = await chat.socket()
  chat.setHistory([message('im-user', 'Asked from a connected channel', 'user'), message('im-reply', 'Reply from a connected channel')])
  await page.clock.runFor(5000)
  await expect(page.locator('article > [data-chat-content]')).toHaveText(['Asked from a connected channel', 'Reply from a connected channel'])
  expect(socket.sent).toEqual([])
  complete(socket.route, message('im-reply', 'Reply from a connected channel'))
  await expect(page.locator('article')).toHaveCount(2)
})

test('older conversations load on demand and no row repeats', async ({ page }) => {
  const chat = await prepareChat(page)
  chat.setSessions(Array.from({ length: 70 }, (_, index) => sessionRow(index + 1)))
  await page.goto(ownPath)
  await chat.socket()
  const conversations = await openConversations(page)
  await expect(conversations.getByRole('link')).toHaveCount(51)
  await expect(conversations.getByRole('link', { name: /^Your conversation/ })).toHaveCount(1)
  await expect(conversations.getByRole('link', { name: /^Older thread 70\b/ })).toHaveCount(0)
  await conversations.getByRole('button', { name: 'Load older conversations' }).click()
  await expect(conversations.getByRole('link', { name: /^Older thread 70\b/ })).toBeVisible()
  await expect(conversations.getByRole('link')).toHaveCount(71)
  await expect(conversations.getByRole('button', { name: /older conversations/ })).toHaveCount(0)
  expect(chat.sessionRequests).toEqual([
    { scope: 'mine', limit: '50', cursor: null },
    { scope: 'mine', limit: '50', cursor: 'after-50' },
  ])
})

test('a session opened by URL beyond the loaded pages resolves through the authorized lookup', async ({ page }) => {
  const chat = await prepareChat(page)
  const deep = { ...sessionRow(200), source_channel: 'agent', writable: true }
  chat.setSessions([...Array.from({ length: 120 }, (_, index) => sessionRow(index + 1)), deep])
  await page.goto(chatPath(deep.id))
  // The lookup, not the first 50 rows, proves this peer-agent session is writable.
  expect((await chat.socket()).session).toBe(deep.id)
  await expect(page.getByRole('textbox', { name: `Message ${originalAgent.name}` })).toBeVisible()
  expect(chat.lookups).toEqual([deep.id])
  expect(chat.sessionRequests).toHaveLength(1)
  // A session on a loaded page needs no lookup and stays read-only because its row says so.
  await navigateChat(page, chatPath(sessionRow(3).id))
  await expect(returnLink(page)).toHaveAttribute('href', ownPath)
  await expect(page.getByRole('textbox', { name: `Message ${originalAgent.name}` })).toHaveCount(0)
  expect(chat.lookups).toEqual([deep.id])
})

test('a lookup the engine refuses leaves a URL-opened session read-only', async ({ page }) => {
  const chat = await prepareChat(page)
  chat.setSessions(Array.from({ length: 60 }, (_, index) => sessionRow(index + 1)))
  const missing = sessionRow(900).id
  await page.goto(chatPath(missing))
  await expect(returnLink(page)).toBeVisible()
  await expect(page.getByRole('textbox', { name: `Message ${originalAgent.name}` })).toHaveCount(0)
  expect(chat.lookups).toEqual([missing])
  expect(chat.sockets).toEqual([])
})
