import { expect, test } from '@playwright/test'
import { chatPath, navigateChat, submitted } from './chat-identity-fixture'
import { prepareChatSkills } from './chat-skill-mentions-fixture'
import { originalAgent, replacementAgent } from './session-fencing-fixture'

const composerName = `Message ${originalAgent.name}`

for (const selection of ['native', 'keyboard'] as const) {
test(`installed-only filtering and click insertion preserve suffix and focus after ${selection} selection`, async ({ page }) => {
  // Given
  const { chat } = await prepareChatSkills(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const composer = page.getByRole('textbox', { name: composerName })
  await composer.fill('Use @au-tail, please')
  switch (selection) {
    case 'native':
      await composer.evaluate((input) => {
        if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the chat textarea')
        input.setSelectionRange(7, 7)
        input.dispatchEvent(new Event('select', { bubbles: true }))
      })
      break
    case 'keyboard':
      for (let index = 7; index < 'Use @au-tail, please'.length; index++) await composer.press('ArrowLeft')
      break
  }
  expect(await composer.evaluate((input) => {
    if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the chat textarea')
    return input.selectionStart
  })).toBe(7)
  const picker = page.getByRole('region', { name: 'Installed skills' })
  await expect(picker.getByRole('button')).toHaveCount(1)
  // When
  await picker.getByRole('button', { name: /^@audit-review / }).click()
  // Then
  await expect(composer).toHaveValue('Use @audit-review , please')
  await expect(composer).toBeFocused()
  expect(await composer.evaluate((input) => {
    if (!(input instanceof HTMLTextAreaElement)) throw new Error('Expected the chat textarea')
    return input.selectionStart
  })).toBe('Use @audit-review '.length)
  await expect(picker).toHaveCount(0)
  expect(socket.sent).toEqual([])
})
}

test('arrow keys and Tab or Enter insert skills instead of sending the message', async ({ page }) => {
  // Given
  const { chat } = await prepareChatSkills(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const composer = page.getByRole('textbox', { name: composerName })
  await composer.fill('@')
  const picker = page.getByRole('region', { name: 'Installed skills' })
  await expect(picker.getByRole('button')).toHaveCount(3)
  await expect(picker.getByText(/private|hidden|no-entry/)).toHaveCount(0)
  // When
  await composer.press('ArrowDown')
  await composer.press('ArrowDown')
  await composer.press('ArrowUp')
  await composer.press('Tab')
  // Then
  await expect(composer).toHaveValue('@quick-start ')
  await expect(composer).toBeFocused()
  expect(socket.sent).toEqual([])
  await composer.fill('@rep')
  await expect(picker.getByRole('button', { name: /^@report / })).toBeVisible()
  await composer.press('Enter')
  await expect(composer).toHaveValue('@report ')
  expect(socket.sent).toEqual([])
})

test('Escape stays dismissed until editing and IME or Shift+Enter never sends', async ({ page }) => {
  // Given
  const { chat } = await prepareChatSkills(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const composer = page.getByRole('textbox', { name: composerName })
  const picker = page.getByRole('region', { name: 'Installed skills' })
  await composer.fill('@audit')
  await expect(picker.getByRole('button')).toHaveCount(1)
  // When
  await composer.press('Escape')
  await composer.press('ArrowLeft')
  await composer.press('ArrowRight')
  await expect(picker).toHaveCount(0)
  await composer.dispatchEvent('compositionstart')
  await composer.press('Enter')
  await composer.dispatchEvent('keydown', { key: 'Enter', keyCode: 229, bubbles: true })
  await composer.dispatchEvent('compositionend')
  await composer.dispatchEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true })
  await composer.press('Backspace')
  await expect(picker.getByRole('button')).toHaveCount(1)
  await composer.press('Shift+Enter')
  // Then
  await expect(composer).toHaveValue('@audi\n')
  expect(socket.sent).toEqual([])
})

test('manual installed mentions send exact skill paths while visible chat retains the original request', async ({ page }) => {
  // Given
  const { chat } = await prepareChatSkills(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const composer = page.getByRole('textbox', { name: composerName })
  await composer.fill('@')
  await expect(page.getByRole('region', { name: 'Installed skills' }).getByRole('button')).toHaveCount(3)
  const draft = '  Run @audit-review and @quick-start; keep @unknown and mail a@b.test.  '
  await composer.fill(draft)
  // When
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  // Then
  await expect.poll(() => socket.sent.length).toBe(1)
  const envelope = submitted(socket)
  expect(envelope.content).toContain('skills/audit-review/SKILL.md')
  expect(envelope.content).toContain('skills/quick-start/skill.md')
  expect(envelope.display_content).toBe(draft.trim())
  expect(envelope.content).not.toBe(draft.trim())
  await expect(page.getByText(draft.trim(), { exact: true })).toBeVisible()
  await expect(page.getByText(envelope.content, { exact: true })).toHaveCount(0)
})

test('ordinary text and unknown or email mentions keep the original content-only protocol', async ({ page }) => {
  // Given
  const { chat } = await prepareChatSkills(page)
  await page.goto(chatPath())
  const socket = await chat.socket()
  const draft = '  Email member@report.test about @catalog-only.  '
  await page.getByRole('textbox', { name: composerName }).fill(draft)
  // When
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  // Then
  await expect.poll(() => socket.sent.length).toBe(1)
  expect(submitted(socket).content).toBe(draft.trim())
  expect(submitted(socket).display_content).toBeUndefined()
})

test('retry reuses the frozen skill envelope after the installed list is removed', async ({ page }) => {
  // Given
  await page.clock.install()
  const fixture = await prepareChatSkills(page)
  await page.goto(chatPath())
  const socket = await fixture.chat.socket()
  const composer = page.getByRole('textbox', { name: composerName })
  await composer.fill('@audit')
  await expect(page.getByRole('button', { name: /^@audit-review / })).toBeVisible()
  await composer.press('Enter')
  await composer.press('Enter')
  await expect.poll(() => socket.sent.length).toBe(1)
  const envelope = submitted(socket)
  socket.route.send(JSON.stringify({ type: 'turn_rejected', client_submission_id: envelope.client_submission_id,
    reason: 'enqueue_failed', content: 'Queue temporarily unavailable.' }))
  const retry = page.getByRole('button', { name: 'Retry this message', exact: true })
  await expect(retry).toBeVisible()
  fixture.clear()
  await page.clock.fastForward(61_000)
  await composer.fill('@')
  await expect(page.getByText('No skills installed for this agent.', { exact: true })).toBeVisible()
  // When
  await retry.click()
  // Then
  await expect.poll(() => socket.sent.length).toBe(2)
  expect(socket.sent[1]).toBe(socket.sent[0])
  expect(envelope.content).toContain('skills/audit-review/SKILL.md')
})

test('loading appears only at the active trigger and agent navigation resets dismissed picker state', async ({ page }) => {
  // Given
  const fixture = await prepareChatSkills(page)
  const gate = fixture.holdNextList()
  await page.goto(chatPath())
  await gate.requested
  const composer = page.getByRole('textbox', { name: composerName })
  try {
    await expect(page.getByText('Loading installed skills…', { exact: true })).toHaveCount(0)
    await composer.fill('@')
    await expect(page.getByText('Loading installed skills…', { exact: true })).toBeVisible()
  } finally { gate.release() }
  await expect(page.getByRole('region', { name: 'Installed skills' }).getByRole('button')).toHaveCount(3)
  await composer.press('Escape')
  // When
  await navigateChat(page, chatPath(undefined, replacementAgent))
  const replacementComposer = page.getByRole('textbox', { name: `Message ${replacementAgent.name}` })
  await replacementComposer.fill('@')
  // Then
  const picker = page.getByRole('region', { name: 'Installed skills' })
  await expect(picker.getByRole('button')).toHaveCount(1)
  await expect(picker.getByRole('button', { name: /^@replacement-only / })).toBeVisible()
})

for (const status of [404, 503]) {
  test(`a ${status} installed list is inline only and never blocks ordinary sending`, async ({ page }) => {
    // Given
    const fixture = await prepareChatSkills(page)
    fixture.setStatus(status)
    await page.goto(chatPath())
    const socket = await fixture.chat.socket()
    const composer = page.getByRole('textbox', { name: composerName })
    await expect(page.getByRole('region', { name: 'Installed skills' })).toHaveCount(0)
    await composer.fill('@')
    const picker = page.getByRole('region', { name: 'Installed skills' })
    await expect(picker.getByText(status === 404 ? 'No skills installed for this agent.' : /Could not load installed skills/)).toBeVisible()
    await composer.fill('Keep ordinary chat working')
    // When
    await composer.press('Enter')
    // Then
    await expect.poll(() => socket.sent.length).toBe(1)
    expect(submitted(socket).content).toBe('Keep ordinary chat working')
    expect(submitted(socket).display_content).toBeUndefined()
  })
}

for (const width of [390, 1440, 1920]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme} installed skill picker`, () => {
      test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, isMobile: width === 390,
        hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme, reducedMotion: 'reduce' })
      test('the scroll-capped picker and active option stay inside the viewport', async ({ page }, testInfo) => {
        // Given
        const fixture = await prepareChatSkills(page)
        for (let index = 0; index < 18; index++) fixture.install(`team-skill-${String(index).padStart(2, '0')}-with-a-long-workspace-folder-name`)
        await page.goto(chatPath())
        const composer = page.getByRole('textbox', { name: composerName })
        await composer.fill('@')
        const picker = page.getByRole('region', { name: 'Installed skills' })
        await expect(picker.getByRole('button')).toHaveCount(21)
        // When
        await composer.press('ArrowUp')
        // Then
        const active = picker.locator('button[aria-current="true"]')
        await expect(active).toBeInViewport()
        const bounds = await picker.boundingBox()
        expect(bounds).not.toBeNull()
        expect(bounds?.x).toBeGreaterThanOrEqual(0)
        expect((bounds?.x ?? 0) + (bounds?.width ?? 0)).toBeLessThanOrEqual(width)
        expect(bounds?.y).toBeGreaterThanOrEqual(0)
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
        await expect(composer).toBeInViewport()
        await page.screenshot({ path: testInfo.outputPath('installed-skill-picker.png'), fullPage: true, animations: 'disabled' })
      })
    })
  }
}
