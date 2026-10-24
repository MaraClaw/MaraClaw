import { expect, test } from '@playwright/test'
import { accept, chatPath, complete, firstSession, message, navigateChat, prepareChat, secondSession, submitted } from './chat-identity-fixture'

import {
  deferCreation, delayedAgent, originalAgent, originalToken, prepareWorkspace, replacementAgent,
  replacementToken, replacementUser, switchFromSettings, tokenKey, writeFromOtherTab,
} from './session-fencing-fixture'

for (const switchToken of [replacementToken, originalToken]) {
  test(`settings switch replaces the primed agents cache (${switchToken === originalToken ? 'same' : 'new'} token)`, async ({ page }) => {
    // Given: navigating within the SPA preserves the old cache unless its owner changes.
    await prepareWorkspace(page, replacementUser, switchToken)
    await page.goto('/app/agents')
    await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()

    // When
    await switchFromSettings(page)

    // Then: wait for the new identity, not a toast or an arbitrary delay.
    await expect(page.getByText(`Signed in as ${replacementUser.display_name}.`, { exact: false })).toBeVisible()
    await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
    const cards = page.locator('main a[href^="/app/agents/"]:not([href="/app/agents/new"])')
    await expect(cards).toHaveCount(1)
    await expect(cards).toHaveAttribute('href', `/app/agents/${replacementAgent.id}/chat`)
    await expect(cards).toContainText(replacementAgent.name)
    await expect(page.getByText(originalAgent.name, { exact: true })).toHaveCount(0)
  })
}

for (const boundary of ['storage replacement', 'sign-out'] as const) {
  for (const outcome of ['success', 'error'] as const) {
    test(`late workspace mutation ${outcome} has no effects after ${boundary}`, async ({ page }) => {
      // Given: hold the real UI's creation request until the old owner is gone.
      await prepareWorkspace(page)
      const creation = await deferCreation(page, outcome)
      await page.goto('/app/agents/new')
      await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
      await page.getByRole('button', { name: 'Create', exact: true }).click()
      await creation.requested
      switch (boundary) {
        case 'storage replacement':
          await writeFromOtherTab(page, replacementToken)
          await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
          await expect(page.getByText(`Signed in as ${replacementUser.display_name}.`, { exact: false })).toBeVisible()
          break
        case 'sign-out':
          await page.getByRole('button', { name: 'Sign out', exact: true }).first().click()
          await expect(page).toHaveURL('/login')
          await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
          break
        default:
          boundary satisfies never
      }
      await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
      await page.clock.pauseAt(new Date('2030-01-01T01:00:00Z'))
      const ownerUrl = page.url()
      const completed = page.waitForEvent('requestfinished', {
        predicate: (request) => request.method() === 'POST' && new URL(request.url()).pathname === '/api/agents/',
      })

      // When
      creation.release()

      // Then: completed transport plus virtual time drains mutation/toast timers, without sleep.
      await completed
      await page.clock.runFor(1_000)
      await expect.soft(page.locator('[data-sonner-toast]'), 'An obsolete mutation must never notify the current owner').toHaveCount(0)
      await expect.soft(page, 'An obsolete success must not navigate to its old-company agent').toHaveURL(ownerUrl)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(
        boundary === 'storage replacement' ? replacementToken : null,
      )
    })
  }
}

for (const outcome of ['success', 'error'] as const) {
  test(`workspace mutation ${outcome} still notifies its current owner`, async ({ page }) => {
    // Given: controls prove the same deferred responses really produce observable effects.
    await prepareWorkspace(page)
    const creation = await deferCreation(page, outcome)
    await page.goto('/app/agents/new')
    await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
    await page.getByRole('button', { name: 'Create', exact: true }).click()
    await creation.requested

    // When
    creation.release()

    // Then
    await expect(page.locator(`[data-sonner-toast][data-type="${outcome}"]`)).toHaveCount(1)
    await expect(page).toHaveURL(outcome === 'success' ? `/app/agents/${delayedAgent.id}/chat` : '/app/agents/new')
  })
}

test('storage sign-out from another real browser tab removes the open workspace', async ({ page }) => {
  // Given
  await prepareWorkspace(page)
  await page.goto('/app/agents')
  await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()

  // When: a native storage event, not a synthetic event or a reload.
  await writeFromOtherTab(page, null)

  // Then
  await expect(page).toHaveURL('/login')
  await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toHaveCount(0)
  await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
})

test('settings switch preserves the replacement identity must-change-password gate', async ({ page }) => {
  // Given
  await prepareWorkspace(page, { ...replacementUser, must_change_password: true })
  await page.goto('/app/agents')
  await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()

  // When
  await switchFromSettings(page)

  // Then: Settings stays accessible, while Agents still redirects to Settings.
  await expect(page).toHaveURL('/app/settings')
  await expect(page.getByText(`Signed in as ${replacementUser.display_name}.`, { exact: false })).toBeVisible()
  await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
  await expect(page).toHaveURL('/app/settings')
  await expect(page.getByLabel('Display name')).toHaveValue(replacementUser.display_name)
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
  await expect(page).toHaveURL('/app/settings')
})

test('equal-text persisted completions remain distinct and repeated UUIDs converge with history first', async ({ page }) => {
  // Given
  const first = message('d289545f-3042-4e6d-94b1-249d6d8a9185', 'Equal persisted reply')
  const second = message('471af989-910d-44c5-a78b-0fc74ede71ca', first.content)
  const chat = await prepareChat(page, [first])
  await page.goto(chatPath())
  const socket = await chat.socket()
  await expect(page.locator('article')).toHaveCount(1)
  // When
  complete(socket.route, second)
  complete(socket.route, second)
  complete(socket.route, first)
  // Then
  await expect(page.locator('article')).toHaveCount(2)
  await expect(page.locator('article')).toHaveText([first.content, second.content])
  chat.setHistory([first, second])
  socket.route.close({ code: 1011 })
  await chat.socket(1)
  await expect.poll(chat.requests).toBeGreaterThan(1)
  await expect(page.locator('article')).toHaveText([first.content, second.content])
})

test('authoritative inline entries replace a whole live parent and reject its late completion', async ({ page }) => {
  // Given
  const whole = message('4214d39b-acf6-41fd-898f-47fa2681e78e', 'Whole live envelope')
  const split = [0, 1].map((index) => ({ ...whole, entry_id: `${whole.message_id}:${index}`, content: 'Equal inline part' }))
  const chat = await prepareChat(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  complete(socket.route, whole)
  await expect(page.locator('article')).toHaveText([whole.content])
  chat.setHistory(split)
  // When
  socket.route.close({ code: 1011 })
  const current = await chat.socket(1)
  await expect(page.locator('article')).toHaveText(split.map((row) => row.content))
  complete(current.route, whole)
  // Then
  await expect(page.locator('article')).toHaveText(split.map((row) => row.content))
  await expect(page.getByText(whole.content, { exact: true })).toHaveCount(0)
})

test('reconnect fetches missed history without dropping older parents or replaying accepted user payloads', async ({ page }) => {
  // Given
  const older = message('older-user', 'Older loaded question', 'user')
  const overlap = message('overlap', 'Loaded reply')
  const sent = message('sent-user', 'Send exactly once', 'user')
  const missed = message('missed-reply', 'Recovered after reconnect')
  const chat = await prepareChat(page, [older, overlap])
  await page.goto(chatPath())
  const socket = await chat.socket()
  await page.getByPlaceholder(`Message ${originalAgent.name}…`).fill(sent.content)
  await page.getByPlaceholder(`Message ${originalAgent.name}…`).press('Enter')
  await expect.poll(() => socket.sent.length).toBe(1)
  const envelope = submitted(socket)
  accept(socket, envelope.client_submission_id)
  chat.setHistory([overlap, { ...sent, client_submission_id: envelope.client_submission_id }, missed])
  // When
  socket.route.close({ code: 1011 })
  const current = await chat.socket(1)
  // Then
  await expect(page.locator('article')).toHaveText([older.content, overlap.content, sent.content, missed.content])
  expect(current.sent).toEqual([])
  expect(envelope.content).toBe(sent.content)
})

for (const boundary of ['session', 'owner'] as const) {
  test(`late reconnect history and old socket events cannot cross the ${boundary} boundary`, async ({ page }) => {
    // Given
    const chat = await prepareChat(page, [message('initial', 'Original loaded reply')])
    await page.goto(chatPath())
    const first = await chat.socket()
    const release = chat.holdNextHistory()
    chat.setHistory([message('obsolete', 'Obsolete reconnect reply')])
    const before = chat.requests()
    first.route.close({ code: 1011 })
    const reconnect = await chat.socket(1)
    await expect.poll(chat.requests).toBeGreaterThan(before)
    switch (boundary) {
      case 'session':
        await navigateChat(page, chatPath(secondSession))
        await expect(page).toHaveURL(chatPath(secondSession))
        break
      case 'owner':
        await writeFromOtherTab(page, replacementToken)
        await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
        await page.getByRole('link', { name: new RegExp(replacementAgent.name) }).click()
        await navigateChat(page, chatPath(secondSession, replacementAgent))
        await expect(page).toHaveURL(chatPath(secondSession, replacementAgent))
        break
      default:
        boundary satisfies never
    }
    await expect.poll(() => chat.sockets.at(-1)?.session).toBe(secondSession)
    const current = await chat.socket(chat.sockets.length - 1)
    expect(current.session).toBe(secondSession)
    await expect(page.getByText('Current second session', { exact: true })).toBeVisible()
    // When
    await release()
    complete(reconnect.route, message('obsolete-frame', 'Obsolete live reply'))
    complete(current.route, message('current-frame', 'Current live reply'))
    // Then
    await expect(page.getByText('Current live reply', { exact: true })).toBeVisible()
    await expect(page.locator('article')).toHaveText(['Current second session', 'Current live reply'])
    expect(first.session).toBe(firstSession)
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
  })
}

for (const boundary of ['logout', 'session', 'owner'] as const) {
  for (const status of [200, 401, 403]) {
    test(`delayed ticket ${status} cannot cross the ${boundary} boundary`, async ({ page }) => {
      const chat = await prepareChat(page)
      const issuance = chat.holdNextTicket()
      await page.goto(chatPath())
      await issuance.requested
      switch (boundary) {
        case 'logout':
          await page.getByRole('button', { name: 'Sign out', exact: true }).first().click()
          await expect(page).toHaveURL('/login')
          break
        case 'session':
          await navigateChat(page, chatPath(secondSession))
          await expect(page).toHaveURL(chatPath(secondSession))
          await chat.socket()
          break
        case 'owner':
          await writeFromOtherTab(page, replacementToken)
          await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
          await page.getByRole('link', { name: new RegExp(replacementAgent.name) }).click()
          await navigateChat(page, chatPath(secondSession, replacementAgent))
          await expect(page).toHaveURL(chatPath(secondSession, replacementAgent))
          await expect.poll(() => chat.sockets.at(-1)?.session).toBe(secondSession)
          break
        default: boundary satisfies never
      }
      const ownerUrl = page.url()
      const sockets = chat.sockets.length
      await issuance.release(status)
      await page.clock.install()
      await page.clock.runFor(1000)
      await expect(page).toHaveURL(ownerUrl)
      expect(chat.sockets.length).toBe(sockets)
      expect(chat.sockets.every((socket) => socket.ticket !== chat.tickets[0]?.ticket)).toBe(true)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(
        boundary === 'logout' ? null : boundary === 'owner' ? replacementToken : originalToken,
      )
    })
  }
}
