import { expect, test } from '@playwright/test'
import { accept, chatPath, complete, firstMessage, firstSession, message, prepareChat, secondSession, submitted } from './chat-identity-fixture'
import { originalAgent, replacementAgent, replacementToken, writeFromOtherTab } from './session-fencing-fixture'

for (const boundary of ['owner', 'conversation', 'agent'] as const) {
  test(`unacknowledged submissions and late receipts cannot cross a retired ${boundary}`, async ({ page }) => {
    const chat = await prepareChat(page)
    if (boundary === 'agent') await page.route('**/api/agents/', (route) => route.fulfill({ json: [originalAgent, replacementAgent] }))
    await page.goto(chatPath())
    const retired = await chat.socket()
    await page.getByRole('textbox', { name: `Message ${originalAgent.name}` }).fill('Private original submission')
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => retired.sent.length).toBe(1)
    const envelope = submitted(retired)
    switch (boundary) {
      case 'owner':
        await writeFromOtherTab(page, replacementToken)
        await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
        await page.getByRole('link', { name: new RegExp(replacementAgent.name) }).click()
        break
      case 'conversation':
        await page.getByRole('button', { name: 'Identity thread 2', exact: true }).click()
        break
      case 'agent':
        await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
        await page.getByRole('link', { name: new RegExp(replacementAgent.name) }).click()
        break
      default: boundary satisfies never
    }
    await chat.socket(1)
    if (boundary === 'owner') await expect.poll(() => chat.tickets.find((ticket) => ticket.ticket === chat.sockets.at(-1)?.ticket)?.agent_id).toBe(replacementAgent.id)
    const current = await chat.socket(chat.sockets.length - 1)
    accept(retired, envelope.client_submission_id)
    retired.route.send(JSON.stringify({ type: 'turn_rejected', client_submission_id: envelope.client_submission_id,
      reason: 'enqueue_failed', content: 'Retired rejection' }))
    complete(retired.route, message('retired-reply', 'Private retired reply'))
    complete(current.route, message('current-reply', 'Current reply'))
    await expect(page.getByText('Current reply', { exact: true })).toBeVisible()
    await expect(page.getByText('Private original submission', { exact: true })).toHaveCount(0)
    await expect(page.getByText('Private retired reply', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Retry this message', exact: true })).toHaveCount(0)
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    expect(current.sent).toEqual([])
    const nextSocket = chat.sockets.length
    current.route.close({ code: 1006 })
    expect((await chat.socket(nextSocket)).sent).toEqual([])
  })
}

for (const boundary of ['owner', 'conversation', 'effect recreation'] as const) {
  test(`an upload completion is fenced by its ${boundary} without losing same-scope uploads`, async ({ page }) => {
    const chat = await prepareChat(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route('**/api/chat/upload', async (route) => {
      requested.resolve()
      await release.promise
      await route.fulfill({ json: { filename: 'late.txt', extracted_text: 'Scoped uploaded content' } })
    })
    await page.goto(chatPath())
    const retired = await chat.socket()
    await page.getByLabel('Attach a file', { exact: true }).setInputFiles({ name: 'late.txt', mimeType: 'text/plain', buffer: Buffer.from('bytes') })
    await requested.promise
    switch (boundary) {
      case 'owner':
        await writeFromOtherTab(page, replacementToken)
        await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
        await page.getByRole('link', { name: new RegExp(replacementAgent.name) }).click()
        break
      case 'conversation':
        await page.getByRole('button', { name: 'Identity thread 2', exact: true }).click()
        break
      case 'effect recreation':
        chat.setOnboarded(true)
        retired.route.send(JSON.stringify({ type: 'onboarded' }))
        break
      default: boundary satisfies never
    }
    await chat.socket(1)
    if (boundary === 'owner') await expect.poll(() => chat.tickets.find((ticket) => ticket.ticket === chat.sockets.at(-1)?.ticket)?.agent_id).toBe(replacementAgent.id)
    const current = await chat.socket(chat.sockets.length - 1)
    const finished = page.waitForEvent('requestfinished', { predicate: (request) => new URL(request.url()).pathname === '/api/chat/upload' })
    release.resolve()
    await finished
    if (boundary === 'effect recreation') {
      await expect.poll(() => current.sent.length).toBe(1)
      expect(submitted(current)).toMatchObject({ content: 'Scoped uploaded content', file_name: 'late.txt', display_content: 'late.txt' })
    } else {
      complete(current.route, message('current', 'Current conversation remains usable'))
      await expect(page.getByText('Current conversation remains usable', { exact: true })).toBeVisible()
      expect(current.sent).toEqual([])
      await expect(page.getByText('Scoped uploaded content', { exact: true })).toHaveCount(0)
    }
    expect(retired.sent).toEqual([])
  })
}

test('late new-session creation cannot navigate or replay across a conversation switch', async ({ page }) => {
  const chat = await prepareChat(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/agents/*/sessions', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { id: firstMessage } })
  })
  await page.goto(chatPath())
  await chat.socket()
  await page.getByRole('button', { name: 'New session', exact: true }).click()
  await requested.promise
  await page.getByRole('button', { name: 'Identity thread 2', exact: true }).click()
  const current = await chat.socket(1)
  const finished = page.waitForEvent('requestfinished', { predicate: (request) => request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/sessions') })
  release.resolve()
  await finished
  complete(current.route, message('current', 'Still in the selected conversation'))
  await expect(page.getByText('Still in the selected conversation', { exact: true })).toBeVisible()
  await expect(page).toHaveURL(chatPath(secondSession))
  expect(current.sent).toEqual([])
})

for (const retireOwner of [false, true]) {
  test(`text queued during session creation ${retireOwner ? 'is discarded for a retired owner' : 'keeps its original logical submission'}`, async ({ page }) => {
    const chat = await prepareChat(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route('**/api/agents/*/sessions?*', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/agents/*/sessions', async (route) => {
      requested.resolve()
      await release.promise
      await route.fulfill({ json: { id: firstSession } })
    })
    await page.goto(`/app/agents/${originalAgent.id}/chat`)
    await expect(page.getByRole('textbox', { name: `Message ${originalAgent.name}` })).toBeVisible()
    await page.getByRole('textbox', { name: `Message ${originalAgent.name}` }).fill('Created once')
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await requested.promise
    if (retireOwner) {
      await writeFromOtherTab(page, replacementToken)
      await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
      await expect(page.getByRole('link', { name: new RegExp(replacementAgent.name) })).toBeVisible()
    }
    const finished = page.waitForEvent('requestfinished', { predicate: (request) => request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/sessions') })
    release.resolve()
    await finished
    if (retireOwner) {
      await expect(page).toHaveURL('/app/agents')
      expect(chat.sockets).toEqual([])
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    } else {
      const socket = await chat.socket()
      await expect.poll(() => socket.sent.length).toBe(1)
      expect(submitted(socket).content).toBe('Created once')
      socket.route.close({ code: 1006 })
      const reconnect = await chat.socket(1)
      await expect.poll(() => reconnect.sent).toEqual(socket.sent)
      await expect(page.locator('article')).toHaveText(['Created once'])
    }
  })
}
