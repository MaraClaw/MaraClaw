import { expect, test } from '@playwright/test'
import { archivedSession, chatPath, message, prepareChat } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'

const sharedPath = `/app/agents/${originalAgent.id}/chat`

for (const width of [390, 1440, 1920]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme} focused chat`, () => {
      test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, colorScheme,
        isMobile: width === 390, hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, reducedMotion: 'reduce' })

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
