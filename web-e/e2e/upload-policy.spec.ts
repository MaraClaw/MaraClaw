import { expect, test } from '@playwright/test'
import { mkdtemp, open, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { ownedUploadBrowser } from './upload-policy-browser-fixture'
import { openUploads, prepareUploads, selectNativeUploadFile, uploadLabel, uploadPolicy } from './upload-policy-fixture'
import { originalAgent } from './session-fencing-fixture'

if (process.env.BROWSER_SKILL_ROOT) test.use({ browser: ownedUploadBrowser })

for (const surface of ['chat', 'workspace'] as const) {
  for (const image of [true, false]) {
    for (const over of [false, true]) {
      test(`${surface} ${image ? 'image' : 'nonimage'} ${over ? 'one byte over is blocked' : 'equality uploads'}`, async ({ page }) => {
        // Given
        const fixture = await prepareUploads(page)
        const limit = surface === 'chat' && image ? uploadPolicy.chat_image_max_bytes : uploadPolicy.workspace_file_max_bytes
        await openUploads(page, surface)
        if (surface === 'chat') await fixture.socket()
        const input = page.getByLabel(uploadLabel(surface), { exact: true })
        await expect(input).toBeEnabled()
        const root = await mkdtemp(join(tmpdir(), 'upload-boundary-'))
        try {
          const filePath = join(root, image ? '.photo.PnG' : 'report.txt')
          const file = await open(filePath, 'w')
          try { await file.truncate(limit + Number(over)) }
          finally { await file.close() }
          // When
          const selectedSize = await selectNativeUploadFile(page, filePath)
          // Then
          expect(selectedSize).toBe(limit + Number(over))
          if (over) {
            await expect(page.locator('[data-sonner-toast]')).toContainText(/exceeds/)
            expect(fixture.uploads).toEqual([])
            expect(fixture.sockets.flatMap((socket) => socket.sent)).toEqual([])
            await expect(input).toHaveValue('')
          } else {
            await expect.poll(() => fixture.uploads.length).toBe(1)
            if (surface === 'chat') await expect.poll(() => fixture.sockets[0]?.sent.length).toBe(1)
            else await expect(page.locator('[data-sonner-toast]')).toContainText('Uploaded')
          }
        } finally { await rm(root, { recursive: true, force: true }) }
      })
    }
  }
}

for (const width of [390, 1440]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme} composer limits`, () => {
      test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, isMobile: width === 390,
        hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme, reducedMotion: 'reduce' })
      test('limits float without moving the editor and support keyboard and outside dismissal', async ({ page }) => {
        // Given
        await prepareUploads(page)
        await page.route('**/api/chat/upload-policy', (route) => route.fulfill({ json: {
          ...uploadPolicy, chat_image_max_bytes: 7, chat_file_max_bytes: 13,
        } }))
        await openUploads(page, 'chat')
        const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
        const trigger = page.getByRole('button', { name: 'Attachment limits', exact: true })
        const before = await composer.boundingBox()
        const formBefore = await page.locator('form').boundingBox()
        await expect(trigger).toHaveAttribute('aria-expanded', 'false')
        // When
        await trigger.click()
        // Then
        const bubble = page.getByRole('dialog', { name: 'Attachment limits' })
        await expect(bubble).toBeVisible()
        await expect(bubble).toContainText('Images (by filename): up to 7 bytes; other files: up to 13 bytes.')
        await expect(trigger).toHaveAttribute('aria-expanded', 'true')
        await expect(trigger).toHaveAttribute('aria-controls', await bubble.getAttribute('id') ?? '')
        expect(await composer.boundingBox()).toEqual(before)
        expect(await page.locator('form').boundingBox()).toEqual(formBefore)
        await expect(bubble).toBeInViewport()
        const bubbleBounds = await bubble.boundingBox()
        expect((bubbleBounds?.y ?? 0) + (bubbleBounds?.height ?? 0)).toBeLessThanOrEqual(before?.y ?? 0)
        await page.keyboard.press('Escape')
        await expect(bubble).toHaveCount(0)
        await expect(trigger).toBeFocused()
        await trigger.press('Enter')
        await expect(bubble).toBeVisible()
        await page.keyboard.press('Escape')
        await trigger.press('Space')
        await expect(bubble).toBeVisible()
        await composer.click()
        await expect(bubble).toHaveCount(0)
        await expect(composer).toBeFocused()
        for (const name of ['Attach a file', 'Attachment limits', 'Send']) {
          const bounds = await page.getByRole('button', { name, exact: true }).boundingBox()
          expect(bounds?.height).toBeGreaterThanOrEqual(44)
          expect(bounds?.width).toBeGreaterThanOrEqual(44)
          expect(bounds?.y).toBeGreaterThanOrEqual((before?.y ?? 0) + (before?.height ?? 0))
        }
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      })
    })
  }
}

test('paperclip opens the native chooser and selection submits then clears the hidden input', async ({ page }) => {
  // Given
  const fixture = await prepareUploads(page)
  await openUploads(page, 'chat')
  const socket = await fixture.socket()
  const input = page.getByLabel('Attach a file', { exact: true })
  await expect(input).toHaveAttribute('type', 'file')
  await expect(input).toBeHidden()
  const chooser = page.waitForEvent('filechooser')
  // When
  await page.getByRole('button', { name: 'Attach a file', exact: true }).click()
  await (await chooser).setFiles({ name: 'report.txt', mimeType: 'text/plain', buffer: Buffer.from('Report') })
  // Then
  await expect.poll(() => fixture.uploads.length).toBe(1)
  await expect.poll(() => socket.sent.length).toBe(1)
  await expect(page.locator('article').filter({ hasText: 'accepted.txt' })).toBeVisible()
  await expect(input).toHaveValue('')
  await expect(page.getByRole('button', { name: 'Attach a file', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled()
})

for (const [filename, mimeType, size, accepted] of [
  ['report.PNG', 'text/plain', 8, false], ['.png', 'image/png', 13, true],
  ['..PNG', 'image/png', 13, true], ['.report.PnG', 'text/plain', 7, true],
  ['report.png.', 'image/png', 13, true], ['report.txt', 'image/png', 13, true],
  ['custom.odd', 'text/plain', 8, false],
] as const) {
  test(`chat uses fetched filename classification for ${filename}`, async ({ page }) => {
    // Given
    const fixture = await prepareUploads(page)
    await page.route('**/api/chat/upload-policy', (route) => route.fulfill({ json: {
      ...uploadPolicy, chat_image_max_bytes: 7, chat_file_max_bytes: 13, image_extensions: ['.png', '.odd'],
    } }))
    await openUploads(page, 'chat')
    await fixture.socket()
    await expect(page.getByLabel('Attach a file', { exact: true })).toBeEnabled()
    // When
    await page.getByLabel('Attach a file', { exact: true }).setInputFiles({ name: filename, mimeType, buffer: Buffer.alloc(size) })
    // Then
    if (accepted) await expect.poll(() => fixture.uploads.length).toBe(1)
    else {
      await expect(page.locator('[data-sonner-toast]')).toContainText(/7 bytes/)
      expect(fixture.uploads).toEqual([])
    }
  })
}

for (const surface of ['chat', 'workspace'] as const) {
  for (const failure of ['HTTP', 'invalid response'] as const) {
    test(`${surface} fails closed on ${failure} and supports keyboard retry`, async ({ page }) => {
      // Given
      const fixture = await prepareUploads(page)
      let attempts = 0
      await page.route('**/api/chat/upload-policy', (route) => {
        attempts++
        return route.fulfill(attempts === 1 ? failure === 'HTTP'
          ? { status: 503, json: { detail: 'Unavailable' } }
          : { json: { ...uploadPolicy, workspace_file_max_bytes: '50' } }
          : { json: { ...uploadPolicy, workspace_file_max_bytes: 23 } })
      })
      await openUploads(page, surface)
      const retry = page.getByRole('button', { name: 'Retry upload limits' })
      await expect(retry).toBeVisible()
      await expect(page.getByRole('alert')).toContainText('Uploading is unavailable')
      await expect(page.getByLabel(uploadLabel(surface), { exact: true })).toBeDisabled()
      // When
      await retry.focus()
      await expect(retry).toBeFocused()
      await retry.press('Enter')
      // Then
      await expect(page.getByLabel(uploadLabel(surface), { exact: true })).toBeEnabled()
      expect(attempts).toBe(2)
      expect(fixture.uploads).toEqual([])
      if (surface === 'workspace') await expect(page.locator('#workspace-upload-policy')).toContainText('23 bytes')
      else await expect(page.locator('#chat-upload-policy')).toContainText('10 MiB')
    })
  }

  test(`${surface} cannot upload while policy is pending`, async ({ page }) => {
    // Given
    const fixture = await prepareUploads(page)
    const release = Promise.withResolvers<void>()
    const requested = Promise.withResolvers<void>()
    await page.route('**/api/chat/upload-policy', async (route) => {
      requested.resolve()
      await release.promise
      await route.fulfill({ json: uploadPolicy })
    })
    try {
      await openUploads(page, surface)
      await requested.promise
      await expect(page.getByLabel(uploadLabel(surface), { exact: true })).toBeDisabled()
      // When: even a programmatic selection cannot bypass the loading gate.
      await page.getByLabel(uploadLabel(surface), { exact: true }).setInputFiles({ name: 'pending.txt', mimeType: 'text/plain', buffer: Buffer.from('bytes') })
      // Then
      expect(fixture.uploads).toEqual([])
    } finally { release.resolve() }
    await expect(page.getByLabel(uploadLabel(surface), { exact: true })).toBeEnabled()
    expect(fixture.uploads).toEqual([])
  })
}
