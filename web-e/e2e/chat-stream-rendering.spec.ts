import { expect, test, type Locator } from '@playwright/test'
import { chatPath, firstMessage, message, navigateChat, prepareChat, secondSession } from './chat-identity-fixture'
import { handled, holdFrames, installFrameClock, paintFrame, sendChunk, sendFrame } from './chat-stream-fixture'
import { originalAgent } from './session-fencing-fixture'
import { originalMarkdown } from './chat-markdown-reference-fixture'

const turn = '11111111-1111-4111-8111-111111111111'
const foreignTurn = '22222222-2222-4222-8222-222222222222'

test('ordinary markdown HTML retains the original escaping and paragraph boundaries', async ({ page }) => {
  const texts = [
    '', '   ', '\n\n', '\n\nend\n\n', 'first\nline\n\n\nsecond',
    '<script>alert("x")</script> & &amp; "quotes"',
    '# Heading **bold** `code`', ' ## Secondary [link](https://example.test/?a=1&b="q") ',
    '### unsupported heading', '- first\n* **second**\nignored\n - indented',
    '```ts\nconst x = "<tag>" & 1\n```', '```',
    '`**nested**` and **`code`**', '[label](http://example.test) [bad](javascript:alert(1))',
    '[`code`](https://example.test/"onclick="x) <img src=x onerror="alert(1)">',
  ]
  const chat = await prepareChat(page, texts.map((text, index) => message(`markdown-${index}`, text)))
  await page.goto(chatPath())
  await chat.socket()
  await page.locator('main [aria-busy]').evaluate((element) => { element.scrollTop = 0 })
  for (const [index, text] of texts.entries()) {
    await page.locator(`[data-chat-entry="markdown-${index}"]`).scrollIntoViewIfNeeded()
    const html = originalMarkdown(text)
    const expected = await page.evaluate((source) => {
      const wrapper = document.createElement('div')
      wrapper.innerHTML = source
      return wrapper.innerHTML
    }, html)
    expect(await page.locator(`[data-chat-entry="markdown-${index}"] > [data-chat-content] > div`).last().evaluate((element) => element.innerHTML), JSON.stringify(text)).toBe(expected)
  }
})

for (const width of [390, 1440]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme} stream retention`, () => {
      test.use({ viewport: { width, height: 900 }, colorScheme, reducedMotion: 'reduce',
        isMobile: width === 390, hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1 })

      test('chunks, typing and history refresh retain historical nodes and completed live blocks', async ({ page }, testInfo) => {
        const history = Array.from({ length: 250 }, (_, index) => message(`retained-${index}`, `History **${index}**\n\nCompleted paragraph ${index}`))
        const chat = await prepareChat(page, history)
        await page.goto(chatPath())
        const socket = await chat.socket()
        await expect(page.locator('[data-chat-entry="retained-249"]')).toBeInViewport()
        for (const count of [100, 150, 200, 250]) {
          const before = chat.requests()
          const earlier = page.getByRole('button', { name: 'Load earlier messages', exact: true })
          await earlier.evaluate((element) => { if (element instanceof HTMLElement) element.focus({ preventScroll: true }) })
          await earlier.press('Enter')
          await expect.poll(chat.requests).toBe(before + 1)
          if (count === 250) await expect(page.getByText('Beginning of conversation', { exact: true })).toHaveCount(1)
          else await expect(earlier).toBeEnabled()
        }
        await page.locator('main [aria-busy]').evaluate((element) => {
          element.scrollTop = element.scrollHeight
          element.dispatchEvent(new Event('scroll'))
        })
        await page.screenshot({ path: testInfo.outputPath('history-before.png') })
        const entries = await page.locator('main [aria-busy]').evaluateHandle((thread) => {
          const bounds = thread.getBoundingClientRect()
          const rows = [...thread.querySelectorAll('[data-chat-entry]')].filter((row) => row.getBoundingClientRect().bottom > bounds.top)
          return rows.map((row) => ({ row, children: [...row.querySelectorAll('*')] }))
        })
        socket.route.send(JSON.stringify({ type: 'chunk', turn_id: turn, seq: 0, content: '# Completed heading\n\nFirst **block**\n\nTrailing' }))
        const live = page.locator('article[aria-live="polite"]')
        await expect(live).toContainText('Trailing')
        const blocks = await live.locator('h3, p').evaluateAll((elements) => elements.length)
        expect(blocks).toBe(3)
        const completed = await live.evaluateHandle((element) => [...element.querySelectorAll('h3, p')].slice(0, 2))
        const mutations = await entries.evaluateHandle((saved) => {
          const records: MutationRecord[] = []
          const observer = new MutationObserver((batch) => records.push(...batch))
          for (const { row } of saved) {
            observer.observe(row, { subtree: true, childList: true, characterData: true, attributes: true })
          }
          return { records, observer }
        })
        for (let seq = 1; seq <= 8; seq++) {
          socket.route.send(JSON.stringify({ type: 'chunk', turn_id: turn, seq, content: ` chunk ${seq}` }))
          await expect(live).toContainText(`chunk ${seq}`)
        }
        await page.getByRole('textbox', { name: `Message ${originalAgent.name}` }).fill('Follow-up while streaming')
        const refreshed = chat.requests()
        socket.route.send(JSON.stringify({ type: 'done', turn_id: foreignTurn, message_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', content: 'Another turn finished' }))
        await expect.poll(chat.requests).toBe(refreshed + 1)
        await expect(page.getByText('Another turn finished', { exact: true })).toBeVisible()
        await expect(live).toContainText('chunk 8')
        expect(await page.locator('main [aria-busy]').evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(2)
        expect(await entries.evaluate((saved) => saved.every(({ row, children }) => row.isConnected && children.every((child) => child.isConnected && row.contains(child))))).toBe(true)
        expect(await completed.evaluate((saved) => saved.every((node) => node.isConnected))).toBe(true)
        expect(await mutations.evaluate(({ records, observer }) => { observer.disconnect(); return records.length })).toBe(0)
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        await page.screenshot({ path: testInfo.outputPath('stream-retained.png') })
        await entries.dispose()
        await completed.dispose()
        await mutations.dispose()

        await navigateChat(page, chatPath(secondSession))
        await expect(page.locator('article > [data-chat-content]')).toHaveText(['Current second session'])
        await expect(live).toHaveCount(0)
      })
    })
  }
}

const replySelector = 'article[aria-live="polite"]'
const threadSelector = 'main [aria-busy]'

const distanceFromBottom = (thread: Locator) => thread.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight)

test('a terminal reply takes effect before the next frame and the unpainted chunks never resurface', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  // Given: one painted frame of a live reply, then chunks the browser has not painted yet.
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const chat = await prepareChat(page, [message('terminal-history', 'Earlier answer')])
  await installFrameClock(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  await expect(page.getByText('Earlier answer', { exact: true })).toBeVisible()
  await holdFrames(page)
  const reply = page.locator(replySelector)
  sendChunk(socket, turn, 0, 'Painted ')
  sendChunk(socket, turn, 1, 'text')
  await handled(page, socket)
  await expect(reply).toHaveCount(0)
  await paintFrame(page)
  await expect(reply).toContainText('Painted text')
  sendChunk(socket, turn, 2, ' and an unpainted tail')
  await handled(page, socket)
  await expect(reply).not.toContainText('unpainted')
  await reply.getByRole('button', { name: 'Copy response', exact: true }).click()
  await expect(reply.getByRole('button', { name: 'Copied response', exact: true })).toBeVisible()
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('Painted text')

  // When: the turn finishes before that frame.
  sendFrame(socket, { type: 'done', turn_id: turn, message_id: firstMessage, content: 'Final answer from the gateway' })

  // Then: the completion replaces the bubble at once, and neither the cancelled frame nor a straggler brings it back.
  await expect(page.getByText('Final answer from the gateway', { exact: true })).toBeVisible()
  await expect(reply).toHaveCount(0)
  sendChunk(socket, turn, 3, ' late straggler')
  await handled(page, socket)
  await paintFrame(page)
  await expect(reply).toHaveCount(0)
  await expect(page.getByText('unpainted')).toHaveCount(0)
  await expect(page.getByText('late straggler')).toHaveCount(0)
  expect(errors).toEqual([])
})

test('a reader scrolled up keeps their place while painted frames grow the reply, and a reader at the bottom follows each frame', async ({ page }) => {
  // Given: a long conversation, with the reader a few screens above the bottom.
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const history = Array.from({ length: 40 }, (_, index) => message(`scroll-${index}`, `Entry ${index}\n\nSecond paragraph ${index}`))
  const chat = await prepareChat(page, history)
  await installFrameClock(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const thread = page.locator(threadSelector)
  const reply = page.locator(replySelector)
  await expect(page.locator('[data-chat-entry="scroll-39"]')).toBeInViewport()
  await holdFrames(page)
  let seq = 0
  sendChunk(socket, turn, seq++, 'Intro paragraph\n\n| Region | Total |\n| --- | ---: |\n| North | 10 |\n| South | 20 |\n\nTail one')
  await handled(page, socket)
  await paintFrame(page)
  await expect(reply).toContainText('Tail one')
  // The first visible entry is read in the same task as the scroll, which is the position the thread's own anchor records.
  const anchor = await thread.evaluate((element) => {
    element.scrollTop -= 400
    element.dispatchEvent(new Event('scroll'))
    const top = element.getBoundingClientRect().top
    const entry = [...element.querySelectorAll<HTMLElement>('[data-chat-entry]')].find((row) => row.getBoundingClientRect().bottom > top)
    if (!entry?.dataset.chatEntry) throw new Error('Expected a visible transcript entry')
    return { id: entry.dataset.chatEntry, top: entry.getBoundingClientRect().top }
  })
  const anchored = page.locator(`[data-chat-entry="${anchor.id}"]`)
  const shift = async () => Math.abs(await anchored.evaluate((element) => element.getBoundingClientRect().top) - anchor.top)
  expect(await distanceFromBottom(thread)).toBeGreaterThan(80)
  const completed = await reply.evaluateHandle((article) => {
    const nodes = [...(article.lastElementChild?.children ?? [])].slice(0, 2)
    const records: MutationRecord[] = []
    const observer = new MutationObserver((batch) => records.push(...batch))
    for (const node of nodes) observer.observe(node, { subtree: true, childList: true, characterData: true, attributes: true })
    return { nodes, records, observer }
  })
  expect(await completed.evaluate(({ nodes }) => nodes.map((node) => node.localName))).toEqual(['p', 'div'])

  // When: each frame paints a burst of chunks below the reader.
  for (let step = 1; step <= 5; step++) {
    const paragraph = `Report paragraph ${step} that keeps the reply growing below the reader`
    const height = await thread.evaluate((element) => element.scrollHeight)
    sendChunk(socket, turn, seq++, `\n\n${paragraph}`)
    sendChunk(socket, turn, seq++, ' (continued)')
    await handled(page, socket)
    // Then: accepted chunks change nothing visible; the frame paints them and the reader's entry has not moved.
    await expect(reply).not.toContainText(paragraph)
    expect(await thread.evaluate((element) => element.scrollHeight)).toBe(height)
    await paintFrame(page)
    await expect(reply).toContainText(`${paragraph} (continued)`)
    expect(await thread.evaluate((element) => element.scrollHeight)).toBeGreaterThan(height)
    await expect.poll(shift, `step ${step}`).toBeLessThanOrEqual(1)
    expect(await distanceFromBottom(thread)).toBeGreaterThan(80)
  }
  expect(await distanceFromBottom(thread)).toBeGreaterThan(80)
  expect(await completed.evaluate(({ nodes, records, observer }) => {
    const mutations = records.length + observer.takeRecords().length
    observer.disconnect()
    return { connected: nodes.every((node) => node.isConnected), mutations }
  })).toEqual({ connected: true, mutations: 0 })
  await completed.dispose()

  // And: once the reader is back at the bottom, the next painted frame is followed, the accepted chunks are not.
  await thread.evaluate((element) => { element.scrollTop = element.scrollHeight; element.dispatchEvent(new Event('scroll')) })
  expect(await distanceFromBottom(thread)).toBeLessThan(2)
  const bottom = await thread.evaluate((element) => element.scrollHeight)
  sendChunk(socket, turn, seq++, `\n\n${Array.from({ length: 6 }, (_, index) => `Follow paragraph ${index}`).join('\n\n')}`)
  await handled(page, socket)
  expect(await thread.evaluate((element) => element.scrollHeight)).toBe(bottom)
  await paintFrame(page)
  await expect(reply).toContainText('Follow paragraph 5')
  expect(await thread.evaluate((element) => element.scrollHeight)).toBeGreaterThan(bottom)
  expect(await distanceFromBottom(thread)).toBeLessThan(2)
  expect(errors).toEqual([])
})

test('switching conversations mid-stream drops the unpainted reply and the next conversation starts clean', async ({ page }) => {
  // Given: chunks accepted for the first conversation that the browser has not painted.
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const chat = await prepareChat(page, [message('switch-history', 'First conversation history')])
  await installFrameClock(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  await expect(page.locator('[data-chat-entry="switch-history"]')).toBeVisible()
  await holdFrames(page)
  const reply = page.locator(replySelector)
  sendChunk(socket, turn, 0, 'Old conversation draft')
  sendChunk(socket, turn, 1, ' with more text')
  await handled(page, socket)
  await expect(reply).toHaveCount(0)

  // When: the member opens another conversation before the frame.
  await navigateChat(page, chatPath(secondSession))
  await expect(page.locator('[data-chat-entry="switch-history"]')).toHaveCount(0)
  await paintFrame(page)

  // Then: the old frame has nothing to paint.
  await expect(reply).toHaveCount(0)
  await expect(page.getByText('Old conversation draft')).toHaveCount(0)

  // And: the new conversation streams from sequence zero of the same turn id, because the old correlation is gone.
  await page.clock.resume()
  await expect(page.getByText('Current second session', { exact: true })).toBeVisible()
  const next = await chat.socket(1)
  sendChunk(next, turn, 0, 'New conversation reply')
  await expect(reply).toContainText('New conversation reply')
  await expect(reply).not.toContainText('Old conversation draft')
  expect(errors).toEqual([])
})

const sample = [
  '# Quarterly report', '',
  'Intro line one', 'Intro with `a|b`, **bold** and <b>markup</b> & "quotes"', '',
  '| Region | Revenue |', '| :--- | ---: |', '| North | `x|y` |', '| South | a \\| b |', 'After the table', '',
  '- first', '- **second** [link](https://example.test/?a=1&b=2)', '', '## Details', '',
  'Pipe | not a table', 'plain follow-up', '',
  '```ts', 'const a = "<tag>" & 1', '', '', 'const b = 2', '```', '',
  'Closing paragraph',
].join('\n')

test('a reply streamed in any chunk size renders exactly like the same text from history', async ({ page }) => {
  // Given: the sample rendered from history, and a clock that paints a frame only when the test says so.
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const chat = await prepareChat(page, [message('sample-history', sample)])
  await installFrameClock(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const history = page.locator('[data-chat-entry="sample-history"] > [data-chat-content] > div').last()
  await expect(history).toBeVisible()
  const expected = await history.evaluate((element) => element.innerHTML)
  expect(expected).toContain('<table')
  expect(expected).toContain('<pre')
  await holdFrames(page)
  const reply = page.locator(replySelector)

  for (const [round, size] of [1, 7, 64].entries()) {
    const id = `00000000-0000-4000-8000-${String(round + 1).padStart(12, '0')}`
    // When: the same text arrives split at every `size` characters, with a frame painted between bursts.
    for (let index = 0; index * size < sample.length; index++) {
      sendChunk(socket, id, index, sample.slice(index * size, (index + 1) * size))
      if ((index + 1) % 40 === 0) {
        await handled(page, socket)
        await paintFrame(page)
      }
    }
    await handled(page, socket)
    await paintFrame(page)
    // Then: the live bubble holds the same markup as history.
    await expect.poll(() => reply.evaluate((article) => article.lastElementChild?.innerHTML), `chunks of ${size}`).toBe(expected)
    sendFrame(socket, { type: 'done', turn_id: id, message_id: `10000000-0000-4000-8000-${String(round + 1).padStart(12, '0')}`, content: sample })
    await expect(reply).toHaveCount(0)
  }
  expect(errors).toEqual([])
})
