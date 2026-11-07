import { expect, test } from '@playwright/test'
import { chatPath, message, navigateChat, prepareChat, secondSession } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'
import { originalMarkdown } from './chat-markdown-reference-fixture'

const turn = '11111111-1111-4111-8111-111111111111'
const foreignTurn = '22222222-2222-4222-8222-222222222222'

test('markdown HTML matches the original renderer including escaping and blank-line boundaries', async ({ page }) => {
  const texts = [
    '', '   ', '\n\n', '\n\nend\n\n', 'first\nline\n\n\nsecond',
    '<script>alert("x")</script> & &amp; "quotes"',
    '# Heading **bold** `code`', ' ## Secondary [link](https://example.test/?a=1&b="q") ',
    '### unsupported heading', '- first\n* **second**\nignored\n - indented',
    '```ts\nconst x = "<tag>" & 1\n```', '```js\na\n\nb\n```', '```c++\n<&>\n```', '```',
    '`**nested**` and **`code`**', '[label](http://example.test) [bad](javascript:alert(1))',
    '[`code`](https://example.test/"onclick="x) <img src=x onerror="alert(1)">',
  ]
  await prepareChat(page, texts.map((text, index) => message(`markdown-${index}`, text)))
  await page.goto(chatPath())
  await expect(page.locator('[data-chat-entry]')).toHaveCount(texts.length)
  for (const [index, text] of texts.entries()) {
    const html = originalMarkdown(text)
    const expected = await page.evaluate((source) => {
      const wrapper = document.createElement('div')
      wrapper.innerHTML = source
      return wrapper.innerHTML
    }, html)
    expect(await page.locator(`[data-chat-entry="markdown-${index}"] > [data-chat-content] > div`).evaluate((element) => element.innerHTML), JSON.stringify(text)).toBe(expected)
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
        await expect(page.locator('[data-chat-entry]')).toHaveCount(50)
        for (const count of [100, 150, 200, 250]) {
          const earlier = page.getByRole('button', { name: 'Load earlier messages', exact: true })
          await earlier.evaluate((element) => { if (element instanceof HTMLElement) element.focus({ preventScroll: true }) })
          await earlier.press('Enter')
          await expect(page.locator('[data-chat-entry]')).toHaveCount(count)
        }
        await page.locator('main [aria-busy]').evaluate((element) => {
          element.scrollTop = element.scrollHeight
          element.dispatchEvent(new Event('scroll'))
        })
        await page.screenshot({ path: testInfo.outputPath('history-before.png') })
        const entries = await page.locator('[data-chat-entry]').first().evaluateHandle((first) => {
          const rows = [...first.parentElement?.querySelectorAll('[data-chat-entry]') ?? []]
          return rows.map((row) => ({ row, children: [...row.querySelectorAll('*')] }))
        })
        socket.route.send(JSON.stringify({ type: 'chunk', turn_id: turn, seq: 0, content: '# Completed heading\n\nFirst **block**\n\nTrailing' }))
        const live = page.locator('article[aria-live="polite"]')
        await expect(live).toContainText('Trailing')
        const blocks = await live.locator('h3, p').evaluateAll((elements) => elements.length)
        expect(blocks).toBe(3)
        const completed = await live.evaluateHandle((element) => [...element.querySelectorAll('h3, p')].slice(0, 2))
        const mutations = await page.locator('[data-chat-entry]').first().evaluateHandle((first) => {
          const records: MutationRecord[] = []
          const observer = new MutationObserver((batch) => records.push(...batch))
          for (const row of first.parentElement?.querySelectorAll('[data-chat-entry]') ?? []) {
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
