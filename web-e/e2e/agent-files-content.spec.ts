import { expect, test, type Page } from '@playwright/test'
import { z } from 'zod'

import type { FileInfo } from '../src/lib/workspace-api'
import { originalAgent, originalToken, prepareWorkspace } from './session-fencing-fixture'

const base = `/api/agents/${originalAgent.id}`
const editorLimit = 1048576
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64')
// Engine FileContent bodies: text decodes as UTF-8, anything else is marked binary and only described.
const reads = {
  'notes.txt': { path: 'notes.txt', content: 'Quarterly notes', version_token: 'notes-v1', size: 15, binary: false },
  'archive.zip': { path: 'archive.zip', content: '[Binary file: archive.zip, 5000 bytes]', version_token: 'zip-v1', size: 5000, binary: true },
  'photo.png': { path: 'photo.png', content: `[Binary file: photo.png, ${png.length} bytes]`, version_token: 'png-v1', size: png.length, binary: true },
} as const
const tooLarge = 'huge.log'
const listing: FileInfo[] = [...Object.keys(reads), tooLarge].map((path) => ({ name: path, path, is_dir: false, size: 1 }))

const textNotice = 'This file (5,000 bytes) is not text, so it cannot be edited here. Download it to view or change it.'
const imageNotice = `This file (${png.length} bytes) is not text, so it cannot be edited here. Download it to view or change it.`
const tooLargeNotice = 'This file is larger than the 1 MiB editor limit. Download it to view or change it.'

async function prepareFiles(page: Page) {
  await prepareWorkspace(page)
  const tickets: { readonly path: string; readonly inline: boolean }[] = []
  const contentReads: string[] = []
  await page.route('**/api/agents/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (!url.pathname.startsWith(base)) return route.fallback()
    if (url.pathname === `${base}/files/download`) {
      // Ticket redemption is a native navigation or image load: the ticket, not a bearer header, authorizes it.
      expect(request.method()).toBe('GET')
      expect(request.headers().authorization).toBeUndefined()
      expect(url.searchParams.get('ticket')).toMatch(/^ticket-\d+$/)
      const path = url.searchParams.get('path') ?? ''
      return url.searchParams.get('inline') === 'true'
        ? route.fulfill({ body: png, contentType: 'image/png' })
        : route.fulfill({ body: 'zip-bytes', contentType: 'application/zip', headers: { 'content-disposition': `attachment; filename="${path}"` } })
    }
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    switch (url.pathname) {
      case base: return route.fulfill({ json: originalAgent })
      case `${base}/metrics`: return route.fulfill({ json: { status: originalAgent.status, tasks: { total: 0, done: 0, pending: 0 } } })
      case `${base}/files/page`:
        expect(request.method()).toBe('GET')
        expect(url.searchParams.get('path')).toBe('')
        return route.fulfill({ json: { items: listing, next_cursor: null } })
      case `${base}/files/content`: {
        expect(request.method()).toBe('GET')
        const path = url.searchParams.get('path') ?? ''
        contentReads.push(path)
        if (path === tooLarge) {
          return route.fulfill({ status: 413, json: { detail: { code: 'file_too_large', message: 'Too large', max_bytes: editorLimit } } })
        }
        const read = Object.entries(reads).find(([name]) => name === path)?.[1]
        expect(read, `Unexpected file read: ${path}`).toBeDefined()
        return route.fulfill({ json: read })
      }
      case `${base}/files/revisions`:
        expect(request.method()).toBe('GET')
        return route.fulfill({ json: [] })
      case `${base}/files/download-ticket`: {
        expect(request.method()).toBe('POST')
        tickets.push(z.object({ path: z.string(), inline: z.boolean() }).strict().parse(request.postDataJSON()))
        return route.fulfill({ json: { ticket: `ticket-${tickets.length}` } })
      }
      default: return route.fallback()
    }
  })
  return { tickets, contentReads }
}

test('a binary file shows a non-editable notice with Download, and switching files keeps the editor state consistent', async ({ page }, testInfo) => {
  // Given
  const fixture = await prepareFiles(page)
  await page.goto(`/app/agents/${originalAgent.id}/files`)
  const save = page.getByRole('button', { name: 'Save', exact: true })
  const editor = (path: string) => page.getByRole('textbox', { name: `Contents of ${path}`, exact: true })
  const notice = page.getByRole('status').filter({ hasText: 'Download it to view or change it.' })
  const noticeDownload = page.getByRole('button', { name: 'Download', exact: true }).nth(1)
  await page.getByRole('button', { name: 'notes.txt', exact: true }).click()
  await expect(editor('notes.txt')).toHaveValue('Quarterly notes')
  await expect(save).toBeEnabled()
  await expect(notice).toHaveCount(0)

  // When a file that is not UTF-8 text opens
  await page.getByRole('button', { name: 'archive.zip', exact: true }).click()

  // Then there is nothing to edit or save, and the notice offers the download
  await expect(notice).toHaveText(textNotice)
  await expect(page.getByRole('textbox')).toHaveCount(0)
  await expect(save).toBeDisabled()
  await expect(noticeDownload).toBeVisible()
  await expect(page.getByText('[Binary file:', { exact: false })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('binary-file-notice.png'), fullPage: true })
  const download = page.waitForEvent('download')
  await noticeDownload.click()
  expect((await download).suggestedFilename()).toBe('archive.zip')
  expect(fixture.tickets).toEqual([{ path: 'archive.zip', inline: false }])

  // When switching to an over-limit file, back to text, and to a binary image
  await page.getByRole('button', { name: tooLarge, exact: true }).click()
  await expect(notice).toHaveText(tooLargeNotice)
  await expect(page.getByRole('textbox')).toHaveCount(0)
  await expect(save).toBeDisabled()
  await page.getByRole('button', { name: 'notes.txt', exact: true }).click()
  await expect(editor('notes.txt')).toHaveValue('Quarterly notes')
  await expect(save).toBeEnabled()
  await expect(notice).toHaveCount(0)
  await page.getByRole('button', { name: 'photo.png', exact: true }).click()

  // Then the image preview still renders above the binary notice
  await expect(page.getByRole('img', { name: 'photo.png', exact: true })).toBeVisible()
  await expect(notice).toHaveText(imageNotice)
  await expect(page.getByRole('textbox')).toHaveCount(0)
  await expect(save).toBeDisabled()
  await page.screenshot({ path: testInfo.outputPath('binary-image-notice.png'), fullPage: true })
  expect(fixture.tickets).toEqual([{ path: 'archive.zip', inline: false }, { path: 'photo.png', inline: true }])
  expect(fixture.contentReads).toEqual(['notes.txt', 'archive.zip', tooLarge, 'notes.txt', 'photo.png'])
})
