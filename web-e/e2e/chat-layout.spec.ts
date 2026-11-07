import { expect, test } from '@playwright/test'
import { archivedSession, chatPath, message, prepareChat } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'

const sharedPath = `/app/agents/${originalAgent.id}/chat`

for (const width of [390, 1440, 1920]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme} focused chat`, () => {
      test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, colorScheme,
        isMobile: width === 390, hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, reducedMotion: 'reduce' })

      test('chat bubbles hug content on both sides with roomy padding, safe tails and wrapping', async ({ page }, testInfo) => {
        // Given: short, multiline and unbroken text from both roles, markdown and an unchanged tool row.
        const chat = await prepareChat(page, [
          message('bubble-short', 'Hi', 'user'),
          message('bubble-multiline', 'First line\nSecond line', 'user'),
          message('bubble-unbroken', 'abcdefghij'.repeat(24), 'user'),
          message('agent-short', 'Hi'),
          message('agent-multiline', 'First line\nSecond line'),
          message('agent-unbroken', 'abcdefghij'.repeat(24)),
          message('bubble-assistant', 'Assistant response with **bold text**.\n\n```ts\n' + 'abcdefghij'.repeat(24) + '\n```'),
          message('bubble-tool', 'Tool response', 'tool'),
        ])
        // When
        await page.goto(chatPath())
        await chat.socket()
        await expect(page.locator('[data-chat-entry="bubble-tool"]')).toBeVisible()
        await page.evaluate(() => document.fonts.ready)
        // Then: each role reserves its tail at the corresponding edge and sizes to its content.
        const thread = page.locator('[aria-busy]')
        const content = await thread.evaluate((element) => {
          const style = getComputedStyle(element)
          return { left: element.getBoundingClientRect().left + parseFloat(style.paddingLeft),
            right: element.getBoundingClientRect().left + element.clientWidth - parseFloat(style.paddingRight),
            width: element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
            cap: 42 * parseFloat(getComputedStyle(document.documentElement).fontSize) }
        })
        for (const entry of ['bubble-short', 'bubble-multiline', 'bubble-unbroken', 'agent-short', 'agent-multiline', 'agent-unbroken', 'bubble-tool']) {
          const row = page.locator(`[data-chat-entry="${entry}"]`)
          const geometry = await row.evaluate((element) => {
            const rect = element.getBoundingClientRect()
            const style = getComputedStyle(element)
            const tail = getComputedStyle(element, '::after')
            const range = document.createRange()
            range.selectNodeContents(element.querySelector('p') ?? element.querySelector('[data-chat-content]') ?? element)
            const footer = element.querySelector('[data-chat-metadata]')
            const metadata = footer?.getBoundingClientRect()
            const time = footer?.querySelector('time')?.getBoundingClientRect()
            const icon = footer?.querySelector('svg')?.getBoundingClientRect()
            return { x: rect.x, width: rect.width, height: rect.height, textWidth: range.getBoundingClientRect().width,
              footerHeight: footer ? footer.getBoundingClientRect().height + parseFloat(getComputedStyle(footer).marginTop) : 0,
              metadataWidth: metadata?.width ?? 0, timeWidth: time?.width ?? 0, iconWidth: icon?.width ?? 0,
              footerRight: metadata?.right, footerBottom: metadata?.bottom, contentBottom: range.getBoundingClientRect().bottom,
              footerTop: metadata?.top, right: rect.right, bottom: rect.bottom,
              paddingX: parseFloat(style.paddingLeft) + parseFloat(style.paddingRight),
              paddingY: parseFloat(style.paddingTop) + parseFloat(style.paddingBottom), lineHeight: parseFloat(style.lineHeight),
              scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
              marginLeft: parseFloat(style.marginLeft), marginRight: parseFloat(style.marginRight),
              fill: style.backgroundColor, tail: { content: tail.content, width: parseFloat(tail.width),
                left: parseFloat(tail.left), right: parseFloat(tail.right), top: parseFloat(tail.top), fill: tail.backgroundColor } }
          })
          const outgoing = ['bubble-short', 'bubble-multiline', 'bubble-unbroken'].includes(entry)
          const tool = entry === 'bubble-tool'
          const tailMargin = outgoing ? geometry.marginRight : geometry.marginLeft
          if (!tool) {
            if (outgoing) expect(geometry.x + geometry.width + tailMargin).toBeCloseTo(content.right, 0)
            else expect(geometry.x - tailMargin).toBeCloseTo(content.left, 0)
            expect(geometry.paddingX).toBe(32)
            expect(geometry.paddingY).toBe(24)
            expect(geometry.tail.content).toBe('""')
            expect(geometry.tail.width).toBeGreaterThan(0)
            expect(geometry.tail.width).toBeLessThanOrEqual(tailMargin)
            expect(outgoing ? geometry.tail.right : geometry.tail.left).toBeCloseTo(-geometry.tail.width, 0)
            expect(geometry.tail.top).toBe(0)
            expect(geometry.tail.fill).toBe(geometry.fill)
            expect(geometry.footerRight).toBeCloseTo(geometry.right - geometry.paddingX / 2, 0)
            expect(geometry.footerBottom).toBeCloseTo(geometry.bottom - geometry.paddingY / 2, 0)
            expect(geometry.footerTop).toBeGreaterThanOrEqual(geometry.contentBottom + 3)
            expect(geometry.timeWidth).toBeGreaterThan(0)
            expect(geometry.iconWidth).toBe(outgoing ? 14 : 0)
          } else {
            expect(geometry.x).toBeCloseTo(content.left, 0)
            expect(geometry.paddingX).toBe(28)
            expect(geometry.paddingY).toBe(20)
            expect(geometry.tail.content).toBe('none')
          }
          expect(geometry.textWidth).toBeLessThanOrEqual(geometry.width - geometry.paddingX + 1)
          expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + (outgoing ? geometry.tail.width : 0))
          if (entry.endsWith('short') || entry.endsWith('multiline')) {
            expect(geometry.width).toBeCloseTo(Math.max(geometry.textWidth, geometry.timeWidth + geometry.iconWidth + (outgoing ? 4 : 0)) + geometry.paddingX, 0)
            expect(geometry.height).toBeCloseTo(geometry.paddingY + geometry.lineHeight * (entry.endsWith('short') ? 1 : 2) + geometry.footerHeight, 0)
          } else expect(geometry.width).toBeCloseTo(Math.min(content.width - (tool ? 0 : tailMargin), content.cap), 0)
        }
        await expect(page.locator('[data-chat-entry="bubble-assistant"] strong')).toHaveText('bold text')
        const code = page.locator('[data-chat-entry="bubble-assistant"] pre')
        expect(await code.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true)
        expect(await code.evaluate((element) => element.getBoundingClientRect().right <= (element.closest('article')?.getBoundingClientRect().right ?? 0))).toBe(true)
        const socket = await chat.socket()
        const turn = '11111111-1111-4111-8111-111111111111'
        socket.route.send(JSON.stringify({ type: 'chunk', turn_id: turn, seq: 0, content: 'Streaming **reply**' }))
        const live = page.locator('article[aria-live="polite"]')
        await expect(live.locator('strong')).toHaveText('reply')
        const streaming = await live.evaluate((element) => {
          const rect = element.getBoundingClientRect(), style = getComputedStyle(element), tail = getComputedStyle(element, '::after')
          return { x: rect.x, width: rect.width, height: rect.height, padding: style.padding, radius: style.borderRadius,
            fill: style.backgroundColor, tail: [tail.left, tail.width, tail.clipPath] }
        })
        expect(streaming.x).toBeCloseTo(content.left + 8, 0)
        expect(streaming.width).toBeLessThan(Math.min(content.width - 8, content.cap))
        socket.route.send(JSON.stringify({ type: 'done', turn_id: turn, message_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', content: 'Streaming **reply**' }))
        await expect(live).toHaveCount(0)
        const settled = page.locator('article').filter({ has: page.getByText('Streaming', { exact: false }) })
        await expect(settled.locator('strong')).toHaveText('reply')
        expect(await settled.evaluate((element) => {
          const rect = element.getBoundingClientRect(), style = getComputedStyle(element), tail = getComputedStyle(element, '::after')
          return { x: rect.x, width: rect.width, height: rect.height, padding: style.padding, radius: style.borderRadius,
            fill: style.backgroundColor, tail: [tail.left, tail.width, tail.clipPath] }
        })).toEqual(streaming)
        expect(await thread.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
        await page.screenshot({ path: testInfo.outputPath('chat-bubbles.png'), fullPage: true })
      })

      for (const [path, reason] of [sharedPath, chatPath(), chatPath(archivedSession)].flatMap((path) =>
        [[path, undefined], [path, null]] as const)) {
        test(`chat fills the workspace without session navigation at ${path} (${reason === null ? 'null' : 'omitted'} access explanation)`, async ({ page }, testInfo) => {
          // Given: archives exist, and a long shared history needs its own scroll container.
          const chat = await prepareChat(page, Array.from({ length: 35 }, (_, index) =>
            message(`layout-${index}`, `History message ${index + 1}: a longer reply that wraps on a phone.`)))
          await page.route(`**/api/agents/${originalAgent.id}`, (route) =>
            route.fulfill({ json: { ...originalAgent, access_reason: reason } }))
          const errors: string[] = []
          page.on('pageerror', (error) => errors.push(error.message))
          // When
          await page.goto(path)
          if (path !== chatPath(archivedSession)) await chat.socket()
          else await expect(page.getByText('Authorized archived history', { exact: true })).toBeVisible()
          // Then: retain both rails, but reclaim all chat header/sidebar space.
          const main = page.getByRole('main')
          const rail = page.getByRole('navigation', { name: 'Agent sections', exact: true })
          await expect(page.getByRole('navigation', { name: 'Workspace', exact: true }).filter({ visible: true })).toBeVisible()
          await expect(rail).toBeVisible()
          await expect(page.getByRole('button', { name: 'Back', exact: true })).toHaveCount(0)
          await expect(page.getByText('Conversations', { exact: true })).toHaveCount(0)
          await expect(page.getByText('Everyone with access to this agent shares this conversation, including messages from connected channels.')).toHaveCount(0)
          await expect(main.getByRole('complementary')).toHaveCount(0)
          await expect(main.getByRole('button', { name: /^(Shared conversation|Archived private thread|Identity thread 2|Edit|Del)$/ })).toHaveCount(0)
          const panel = main.locator('section').filter({ has: page.locator('[aria-busy]') })
          const bounds = await panel.boundingBox()
          const mainBounds = await main.boundingBox()
          const railBounds = await rail.boundingBox()
          if (!bounds || !mainBounds || !railBounds) throw new Error('Expected bounded chat and rails')
          expect(Math.abs(bounds.x - (width < 768 ? mainBounds.x : railBounds.x + railBounds.width))).toBeLessThanOrEqual(1)
          expect(Math.abs(bounds.y - (width < 768 ? railBounds.y + railBounds.height : mainBounds.y))).toBeLessThanOrEqual(1)
          expect(Math.abs(bounds.x + bounds.width - mainBounds.x - mainBounds.width)).toBeLessThanOrEqual(1)
          expect(Math.abs(bounds.y + bounds.height - mainBounds.y - mainBounds.height)).toBeLessThanOrEqual(1)
          if (path === chatPath(archivedSession)) {
            await expect(page.getByText('Archived conversation. Read-only history.', { exact: true })).toBeVisible()
            await expect(page.getByRole('link', { name: 'Return to shared conversation', exact: true })).toHaveAttribute('href', sharedPath)
            await expect(page.getByRole('textbox')).toHaveCount(0)
            expect(chat.sockets).toEqual([])
          } else {
            const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
            await expect(composer).toBeVisible()
            const form = panel.locator('form')
            const composerBounds = await form.boundingBox()
            if (!composerBounds) throw new Error('Expected chat composer bounds')
            expect(Math.abs(composerBounds.y + composerBounds.height - bounds.y - bounds.height)).toBeLessThanOrEqual(1)
            const thread = panel.locator('[aria-busy]')
            const threadBounds = await thread.boundingBox()
            if (!threadBounds) throw new Error('Expected chat thread bounds')
            expect(Math.abs(threadBounds.y - bounds.y)).toBeLessThanOrEqual(1)
            expect(await thread.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)
            await page.screenshot({ path: testInfo.outputPath('focused-chat-start.png'), fullPage: true })
            await thread.evaluate((element) => { element.scrollTop = element.scrollHeight })
            await expect(page.getByText('History message 35: a longer reply that wraps on a phone.', { exact: true })).toBeInViewport()
            await expect(composer).toBeInViewport()
            await expect(rail).toBeInViewport()
            expect(await main.evaluate((element) => element.scrollTop)).toBe(0)
          }
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
          expect(await page.evaluate(() => ({ width: innerWidth, dpr: devicePixelRatio, touch: navigator.maxTouchPoints > 0 })))
            .toEqual({ width, dpr: width === 390 ? 3 : 1, touch: width === 390 })
          expect(errors).toEqual([])
          await page.screenshot({ path: testInfo.outputPath('focused-chat.png'), fullPage: true })
        })
      }
    })
  }
}

test('loading and unavailable chat retain rails and Agents recovery without Back', async ({ page }) => {
  // Given
  await prepareChat(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**/api/agents/${originalAgent.id}`, async (route) => {
    requested.resolve()
    await release.promise
    await route.fulfill({ status: 403, json: { detail: 'Agent access denied' } })
  })
  // When
  await page.goto(sharedPath)
  await requested.promise
  try {
    // Then
    await expect(page.getByRole('navigation', { name: 'Agent sections', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Back', exact: true })).toHaveCount(0)
  } finally {
    release.resolve()
  }
  await expect(page.getByText('Agent access denied', { exact: true })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Agent sections', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Back', exact: true })).toHaveCount(0)
  const recovery = page.getByRole('main').getByRole('link', { name: 'Agents', exact: true })
  expect(await recovery.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44)
  await recovery.click()
  await expect(page).toHaveURL('/app/agents')
  await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()
})
