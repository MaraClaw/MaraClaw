import { expect, test } from '@playwright/test'
import { openVideoChat, prepareVideoChat } from './attachment-video-fixture'
import { chatPath } from './chat-identity-fixture'

test('video plays only on demand and closes with a released source and object URL', async ({ page }, testInfo) => {
  const fixture = await openVideoChat(page)
  const video = page.locator('video')
  await expect(video).toBeVisible()
  await expect(video).toHaveAttribute('preload', 'none')
  await expect(video).toHaveAttribute('playsinline', '')
  expect(await video.evaluate((element: HTMLVideoElement) => ({ controls: element.controls, autoplay: element.autoplay, paused: element.paused })))
    .toEqual({ controls: true, autoplay: false, paused: true })
  const element = await video.elementHandle()
  if (!element) throw new Error('Expected mounted video')
  const source = await video.getAttribute('src')
  expect(source).toMatch(/^blob:/)
  await video.evaluate((element: HTMLVideoElement) => element.play())
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime)).toBeGreaterThan(0)
  await page.screenshot({ path: testInfo.outputPath('video-playing.png') })
  await page.getByRole('button', { name: 'Close preview', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  // load() without a source reports NETWORK_NO_SOURCE until Chromium's resource-selection task settles it to NETWORK_EMPTY.
  await expect.poll(() => element.evaluate((video) => ({ src: video.getAttribute('src'), paused: video.paused, networkState: video.networkState })))
    .toEqual({ src: null, paused: true, networkState: 0 })
  expect(await page.evaluate(() => document.documentElement.dataset.revokedPreviewUrl)).toBe(source)
  await page.getByRole('button', { name: 'Preview', exact: true }).click()
  await expect(video).toBeVisible()
  expect(fixture.metadataCalls()).toBe(2)
  expect(fixture.byteCalls()).toBe(1)
  expect(await video.getAttribute('src')).not.toBe(source)
})

for (const [name, metadata, guidance] of [
  ['unsupported MIME', { content_type: 'video/quicktime' }, 'Preview is not supported for this file type. Download it to view it.'],
  ['oversized video', { size: 20 * 1024 * 1024 + 1 }, 'Preview is limited to files up to 20 MiB. Download this file to view it.'],
  ['legacy DOC', { name: 'report.doc', content_type: 'application/msword' }, 'Preview is not supported for this file type. Download it to view it.'],
  ['legacy XLS', { name: 'report.xls', content_type: 'application/vnd.ms-excel' }, 'Preview is not supported for this file type. Download it to view it.'],
] as const) {
  test(`${name} keeps download guidance without requesting preview bytes`, async ({ page }) => {
    const fixture = await openVideoChat(page, metadata)
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText(guidance, { exact: true })).toBeVisible()
    await expect(dialog.getByRole('button', { name: 'Download', exact: true })).toBeEnabled()
    await expect(page.locator('video')).toHaveCount(0)
    expect(fixture.metadataCalls()).toBe(1)
    expect(fixture.byteCalls()).toBe(0)
  })
}

test('a browser media error displays download guidance instead of a broken player', async ({ page }) => {
  await prepareVideoChat(page)
  await page.route('**/files/preview-bytes?*', (route) => route.fulfill({ body: 'invalid media bytes' }))
  await page.goto(chatPath())
  await page.getByRole('button', { name: 'Preview', exact: true }).click()
  await page.locator('video').evaluate((element: HTMLVideoElement) => element.play().catch((error: unknown) => {
    if (error instanceof DOMException) return error.name
    throw error
  }))
  await expect(page.getByRole('alert')).toHaveText('Preview could not be displayed. Download the file to view it.')
  await expect(page.locator('video')).toBeHidden()
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Download', exact: true })).toBeEnabled()
})
