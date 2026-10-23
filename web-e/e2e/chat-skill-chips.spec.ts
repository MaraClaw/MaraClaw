import { expect, test, type Locator } from '@playwright/test'
import { buildSkillMentionContent } from '../src/lib/chat/skill-mentions'
import { chatPath, submitted } from './chat-identity-fixture'
import { prepareChatSkills } from './chat-skill-mentions-fixture'
import { originalAgent } from './session-fencing-fixture'

const composerName = `Message ${originalAgent.name}`
const chipSelector = '[data-slot="skill-mention-chip"]'

async function chipGeometry(composer: Locator) {
  return composer.evaluate((input) => {
    if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the native composer')
    const backdrop = input.parentElement?.querySelector('[data-slot="skill-mention-backdrop"]')
    if (!(backdrop instanceof HTMLDivElement)) throw new Error('Expected the chip backdrop')
    const style = getComputedStyle(input)
    const bounds = input.getBoundingClientRect()
    const probe = document.createElement('div')
    for (const property of ['box-sizing', 'font-family', 'font-size', 'font-weight', 'font-style', 'line-height',
      'letter-spacing', 'word-spacing', 'text-indent', 'text-transform', 'text-align', 'tab-size',
      'padding', 'border-width', 'border-style', 'white-space', 'word-break', 'overflow-wrap',
      'overflow-x', 'overflow-y', 'scrollbar-gutter']) probe.style.setProperty(property, style.getPropertyValue(property))
    Object.assign(probe.style, { position: 'fixed', left: `${bounds.left}px`, top: `${bounds.top}px`,
      width: `${bounds.width}px`, height: `${bounds.height}px`, visibility: 'hidden', pointerEvents: 'none' })
    probe.textContent = `${input.value}\n`
    document.body.append(probe)
    probe.scrollTop = input.scrollTop
    probe.scrollLeft = input.scrollLeft
    try {
      const text = probe.firstChild
      if (!text) throw new Error('Expected a text geometry probe')
      const rectangles = [...backdrop.querySelectorAll('[data-slot="skill-mention-chip"]')].map((chip) => {
        const prefix = document.createRange()
        prefix.setStart(backdrop, 0)
        prefix.setEndBefore(chip)
        const start = prefix.toString().length
        const range = document.createRange()
        range.setStart(text, start)
        range.setEnd(text, start + (chip.textContent?.length ?? 0))
        const actual = [...chip.getClientRects()]
        const expected = [...range.getClientRects()]
        return { count: actual.length, expectedCount: expected.length, deltas: actual.map((rect, index) => {
          const target = expected[index]
          if (!target) return [Infinity]
          return [rect.x - target.x, rect.y - target.y, rect.width - target.width, rect.height - target.height]
        }) }
      })
      return { rectangles, scrollTop: input.scrollTop, mirrorScrollTop: backdrop.scrollTop,
        scrollHeight: input.scrollHeight, mirrorScrollHeight: backdrop.scrollHeight,
        value: input.value, mirrored: backdrop.textContent, color: getComputedStyle(backdrop).color,
        pointerEvents: getComputedStyle(backdrop).pointerEvents, ariaHidden: backdrop.getAttribute('aria-hidden') }
    } finally { probe.remove() }
  })
}

test('picker insertion paints one accent chip without replacing the native textbox or moving its caret', async ({ page }) => {
  // Given
  const { chat } = await prepareChatSkills(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const composer = page.getByRole('textbox', { name: composerName })
  await composer.fill('Use @au-tail, please')
  await composer.evaluate((input) => {
    if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the native composer')
    input.setSelectionRange(7, 7)
    input.dispatchEvent(new Event('select', { bubbles: true }))
  })
  // When
  await page.getByRole('region', { name: 'Installed skills' }).getByRole('button', { name: /^@audit-review / }).click()
  // Then
  await expect(composer).toHaveValue('Use @audit-review , please')
  await expect(composer).toBeFocused()
  await expect(page.getByRole('textbox')).toHaveCount(1)
  await expect(page.locator(chipSelector)).toHaveText(['@audit-review'])
  expect(await composer.evaluate((input) => {
    if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the native composer')
    return [input.selectionStart, input.selectionEnd]
  })).toEqual([18, 18])
  const geometry = await chipGeometry(composer)
  expect(geometry.ariaHidden).toBe('true')
  expect(geometry.pointerEvents).toBe('none')
  expect(geometry.color).toBe('rgba(0, 0, 0, 0)')
  expect(socket.sent).toEqual([])
})

test('manually typed exact chips share submission resolution including dotted names and repeated punctuation', async ({ page }) => {
  // Given
  const fixture = await prepareChatSkills(page)
  fixture.install('report.v2')
  await page.goto(chatPath())
  const socket = await fixture.chat.socket()
  const composer = page.getByRole('textbox', { name: composerName })
  await composer.fill('@')
  await expect(page.getByRole('region', { name: 'Installed skills' }).getByRole('button')).toHaveCount(4)
  const draft = '  Use @audit-review, @report.v2...\nThen @report! @report? Keep @unknown member@report @REPORT.  '
  // When
  await composer.fill(draft)
  // Then
  await expect(page.locator(chipSelector)).toHaveText(['@audit-review', '@report.v2', '@report', '@report'])
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect.poll(() => socket.sent.length).toBe(1)
  const { client_submission_id, ...payload } = submitted(socket)
  expect(client_submission_id).toBeTruthy()
  expect(payload).toEqual(buildSkillMentionContent(draft, [
    { name: 'audit-review', path: 'skills/audit-review/SKILL.md' },
    { name: 'report.v2', path: 'skills/report.v2/SKILL.md' },
    { name: 'report', path: 'skills/report.md' },
  ]))
  await expect(composer).toHaveValue('')
  await expect(page.locator(chipSelector)).toHaveCount(0)
})

test('native editing deletes a chip and preserves selected ranges while plain unknown and email text stay unstyled', async ({ page }) => {
  // Given
  await prepareChatSkills(page)
  await page.goto(chatPath())
  const composer = page.getByRole('textbox', { name: composerName })
  await composer.fill('@')
  await expect(page.getByRole('region', { name: 'Installed skills' }).getByRole('button')).toHaveCount(3)
  await composer.fill('Use @report and @unknown member@report @REPORT plain')
  await expect(page.locator(chipSelector)).toHaveText(['@report'])
  await composer.evaluate((input) => {
    if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the native composer')
    input.setSelectionRange(4, 11, 'backward')
    input.dispatchEvent(new Event('select', { bubbles: true }))
  })
  // When
  await composer.press('Backspace')
  // Then
  await expect(composer).toHaveValue('Use  and @unknown member@report @REPORT plain')
  await expect(page.locator(chipSelector)).toHaveCount(0)
  expect(await composer.evaluate((input) => {
    if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the native composer')
    return [input.selectionStart, input.selectionEnd]
  })).toEqual([4, 4])
})

for (const width of [390, 1440, 1920]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme} inline skill chips`, () => {
      test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, isMobile: width === 390,
        hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme, reducedMotion: 'reduce' })
      test('backgrounds align with native wrapped text and follow scroll without shifting the selection', async ({ page }, testInfo) => {
        // Given
        const fixture = await prepareChatSkills(page)
        const longName = `team-${'long-workspace-'.repeat(12)}review`
        fixture.install(longName)
        await page.goto(chatPath())
        const composer = page.getByRole('textbox', { name: composerName })
        await composer.fill('@')
        await expect(page.getByRole('region', { name: 'Installed skills' }).getByRole('button')).toHaveCount(4)
        const draft = `Start @audit-review, then @${longName}.\n${'Native wrapping around @report and ordinary words. '.repeat(16)}\nLast @report.\n`
        await composer.fill(draft)
        await composer.press('Escape')
        await composer.evaluate((input) => {
          if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the native composer')
          input.setSelectionRange(2, 9, 'backward')
          input.dispatchEvent(new Event('select', { bubbles: true }))
        })
        // When
        await composer.evaluate((input) => {
          input.scrollTop = input.scrollHeight
          input.dispatchEvent(new Event('scroll'))
        })
        // Then
        const geometry = await chipGeometry(composer)
        expect(geometry.mirrored).toBe(`${draft}\n`)
        expect(geometry.scrollTop).toBeGreaterThan(0)
        expect(geometry.mirrorScrollTop).toBe(geometry.scrollTop)
        expect(geometry.mirrorScrollHeight).toBe(geometry.scrollHeight)
        expect(geometry.rectangles.length).toBe(19)
        expect(geometry.rectangles.some((rect) => rect.count > 1)).toBe(true)
        for (const rectangle of geometry.rectangles) {
          expect(rectangle.count).toBe(rectangle.expectedCount)
          for (const delta of rectangle.deltas.flat()) expect(Math.abs(delta)).toBeLessThan(0.75)
        }
        expect(await composer.evaluate((input) => {
          if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the native composer')
          return [input.selectionStart, input.selectionEnd, input.selectionDirection]
        })).toEqual([2, 9, 'backward'])
        expect(await page.locator(chipSelector).first().evaluate((chip) => {
          const token = document.createElement('span')
          token.style.backgroundColor = 'var(--accent)'
          document.body.append(token)
          try { return getComputedStyle(chip).backgroundColor === getComputedStyle(token).backgroundColor }
          finally { token.remove() }
        })).toBe(true)
        await expect(page.getByRole('textbox')).toHaveCount(1)
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        await page.screenshot({ path: testInfo.outputPath('skill-chips-scrolled.png'), fullPage: true, animations: 'disabled' })
        await composer.evaluate((input) => { input.scrollTop = 0; input.dispatchEvent(new Event('scroll')) })
        await page.screenshot({ path: testInfo.outputPath('skill-chips-wrapped.png'), fullPage: true, animations: 'disabled' })
      })
    })
  }
}
