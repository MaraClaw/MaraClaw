import { expect, test, type Locator } from '@playwright/test'
import { chatPath, complete, firstSession, message, navigateChat, prepareChat, secondSession } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'

const rows = (count: number) => Array.from({ length: count }, (_, index) => message(`history-${index}`, `History entry ${index}`))
const threadSelector = 'main [aria-busy]'

async function visibleEntry(thread: Locator) {
  return thread.evaluate((element) => {
    const top = element.getBoundingClientRect().top
    const entry = [...element.querySelectorAll<HTMLElement>('[data-chat-entry]')].find((row) => row.getBoundingClientRect().bottom > top)
    if (!entry?.dataset.chatEntry) throw new Error('Expected a visible transcript entry')
    return { id: entry.dataset.chatEntry, top: entry.getBoundingClientRect().top }
  })
}

async function activateEarlier(button: Locator) {
  await button.evaluate((element) => { if (element instanceof HTMLElement) element.focus({ preventScroll: true }) })
  await button.press('Enter')
}

test('newest messages start at the bottom and one near-top load prepends without moving the visible entry', async ({ page }, testInfo) => {
  // Given
  const history = rows(150)
  const chat = await prepareChat(page, history)
  await page.goto(chatPath())
  await chat.socket()
  const thread = page.locator(threadSelector)
  await expect(page.locator('article')).toHaveText(history.slice(100).map((row) => row.content))
  await expect(page.getByText('History entry 149', { exact: true })).toBeInViewport()
  expect(await thread.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(2)
  const release = chat.holdNextHistory()
  const before = chat.requests()
  const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
  await composer.focus()
  // When
  await thread.evaluate((element) => { element.scrollTop = 120 })
  await expect.poll(chat.requests).toBe(before + 1)
  await expect(page.getByRole('button', { name: 'Loading earlier messages…', exact: true })).toBeDisabled()
  await expect(composer).toBeFocused()
  const anchor = await visibleEntry(thread)
  await release()
  // Then
  await expect(page.locator('article')).toHaveText(history.slice(50).map((row) => row.content))
  const anchored = page.locator(`[data-chat-entry="${anchor.id}"]`)
  expect(Math.abs(await anchored.evaluate((element) => element.getBoundingClientRect().top) - anchor.top)).toBeLessThanOrEqual(1)
  expect(chat.requests()).toBe(before + 1)
  await expect(page.getByRole('button', { name: 'Load earlier messages', exact: true })).toBeEnabled()
  await expect(composer).toBeFocused()
  await page.screenshot({ path: testInfo.outputPath('history-anchored.png'), fullPage: true })
})

test('keyboard earlier loading can retry a failure while the existing socket remains writable, then reports exhaustion', async ({ page }) => {
  // Given
  const history = rows(80)
  const chat = await prepareChat(page, history)
  let attempts = 0
  const releaseFailure = Promise.withResolvers<void>()
  await page.route('**/messages/page?*', async (route) => {
    if (new URL(route.request().url()).searchParams.has('cursor') && ++attempts === 1) {
      await releaseFailure.promise
      return route.fulfill({ status: 503, json: { detail: 'Temporary history failure' } })
    }
    return route.fallback()
  })
  await page.goto(chatPath())
  const socket = await chat.socket()
  try {
    await activateEarlier(page.getByRole('button', { name: 'Load earlier messages', exact: true }))
    const loading = page.getByRole('button', { name: 'Loading earlier messages…', exact: true })
    await expect(loading).toBeFocused()
    await expect(loading).toBeDisabled()
    await loading.press('Enter')
    await loading.press('Space')
    expect(attempts).toBe(1)
  } finally {
    releaseFailure.resolve()
  }
  await expect(page.getByRole('alert')).toHaveText('Could not load earlier messages. You can keep chatting.')
  const retry = page.getByRole('button', { name: 'Retry earlier messages', exact: true })
  await expect(retry).toBeFocused()
  const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
  await composer.fill('Writable after history failure')
  await composer.press('Enter')
  await expect.poll(() => socket.sent.length).toBe(1)
  // When
  await activateEarlier(retry)
  // Then
  await expect(page.locator('article')).toHaveText([...history.map((row) => row.content), 'Writable after history failure'])
  await expect(page.getByText('Beginning of conversation', { exact: true })).toHaveCount(1)
  await expect(page.getByRole('button', { name: /earlier messages/i })).toHaveCount(0)
  expect(attempts).toBe(2)
  expect(chat.sockets).toHaveLength(1)
})

test('a cached revisit keeps earlier pages and bridges a refresh gap larger than two pages', async ({ page }) => {
  // Given
  const history = rows(125)
  const chat = await prepareChat(page, history)
  const cursors: (string | null)[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname.endsWith(`/sessions/${firstSession}/messages/page`)) cursors.push(url.searchParams.get('cursor'))
  })
  await page.goto(chatPath())
  await chat.socket()
  await activateEarlier(page.getByRole('button', { name: 'Load earlier messages', exact: true }))
  await expect(page.locator('article')).toHaveText(history.slice(25).map((row) => row.content))
  await navigateChat(page, chatPath(secondSession))
  await expect(page.getByText('Current second session', { exact: true })).toBeVisible()
  const expanded = rows(270)
  chat.setHistory(expanded)
  const release = chat.holdNextHistory()
  const before = cursors.length
  // When
  await navigateChat(page, chatPath())
  await expect(page.locator('article')).toHaveText(history.slice(25).map((row) => row.content))
  await expect.poll(() => cursors.length).toBe(before + 1)
  await release()
  // Then
  await expect(page.locator('article')).toHaveText(expanded.slice(25).map((row) => row.content))
  expect(cursors.slice(before)).toEqual([null, 'history-220', 'history-170'])
  await expect(page.getByText('History entry 269', { exact: true })).toBeInViewport()
  await expect(page.getByRole('button', { name: 'Load earlier messages', exact: true })).toBeEnabled()
})

test('live completions preserve the reading position away from the bottom and follow when already at the bottom', async ({ page }) => {
  // Given
  const history = rows(75)
  const chat = await prepareChat(page, history)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const thread = page.locator(threadSelector)
  await thread.evaluate((element) => { element.scrollTop = 300; element.dispatchEvent(new Event('scroll')) })
  const anchor = await visibleEntry(thread)
  const first = message('live-first', 'New reply while reading earlier')
  chat.setHistory([...history, first])
  // When
  complete(socket.route, first)
  // Then
  await expect(page.locator('article')).toHaveCount(51)
  const anchored = page.locator(`[data-chat-entry="${anchor.id}"]`)
  expect(Math.abs(await anchored.evaluate((element) => element.getBoundingClientRect().top) - anchor.top)).toBeLessThanOrEqual(1)
  expect(await thread.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeGreaterThan(80)
  await thread.evaluate((element) => { element.scrollTop = element.scrollHeight; element.dispatchEvent(new Event('scroll')) })
  const second = message('live-second', 'New reply at the bottom')
  chat.setHistory([...history, first, second])
  complete(socket.route, second)
  await expect(page.locator('article')).toHaveCount(52)
  await expect(page.getByText(second.content, { exact: true })).toBeInViewport()
  expect(await thread.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(2)
})

test('a delayed earlier page cannot appear after switching sessions', async ({ page }) => {
  // Given
  const chat = await prepareChat(page, rows(125))
  await page.goto(chatPath())
  await chat.socket()
  const release = chat.holdNextHistory()
  const before = chat.requests()
  await activateEarlier(page.getByRole('button', { name: 'Load earlier messages', exact: true }))
  await expect.poll(chat.requests).toBe(before + 1)
  // When
  try {
    await navigateChat(page, chatPath(secondSession))
    await expect(page.getByText('Current second session', { exact: true })).toBeVisible()
  } finally {
    await release()
  }
  // Then
  await expect(page.locator('article')).toHaveText(['Current second session'])
  await expect(page.getByText('Beginning of conversation', { exact: true })).toHaveCount(1)
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('initial history loading and failure offer explicit recovery before opening the writable socket', async ({ page }) => {
  // Given
  const chat = await prepareChat(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  let attempts = 0
  await page.route('**/messages/page?*', async (route) => {
    if (++attempts > 1) return route.fallback()
    requested.resolve()
    await release.promise
    return route.fulfill({ status: 503, json: { detail: 'Temporary history failure' } })
  })
  await page.goto(chatPath())
  await requested.promise
  try {
    await expect(page.getByText('Loading conversation…', { exact: true })).toBeVisible()
    expect(chat.sockets).toHaveLength(0)
  } finally {
    release.resolve()
  }
  await expect(page.getByRole('alert')).toBeVisible()
  // When
  await page.getByRole('button', { name: 'Retry conversation', exact: true }).click()
  // Then
  await chat.socket()
  await expect(page.getByText('Beginning of conversation', { exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})
