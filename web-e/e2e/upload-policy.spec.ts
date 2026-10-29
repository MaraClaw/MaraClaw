import { expect, test } from '@playwright/test'
import { mkdtemp, open, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { ownedUploadBrowser } from './upload-policy-browser-fixture'
import { openUploads, prepareUploads, selectNativeUploadFile, uploadLabel, uploadPolicy } from './upload-policy-fixture'

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
