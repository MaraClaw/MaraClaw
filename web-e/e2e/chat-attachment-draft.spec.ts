import { expect, test } from '@playwright/test'
import { accept, submitted } from './chat-identity-fixture'
import { originalAgent, originalToken } from './session-fencing-fixture'
import { openUploads, prepareUploads } from './upload-policy-fixture'

test('removing a staged attachment leaves a normal text-only send', async ({ page }) => {
  const fixture = await prepareUploads(page)
  await openUploads(page, 'chat')
  const socket = await fixture.socket()
  const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
  const text = 'Send this without a file.'
  await composer.fill(text)
  const input = page.getByLabel('Attach a file', { exact: true })
  await expect(input).toBeEnabled()
  await input.setInputFiles({ name: 'report.txt', mimeType: 'text/plain', buffer: Buffer.from('Report') })
  const staged = page.locator('form').getByText('report.txt', { exact: true })
  await expect(staged).toBeVisible()

  await page.getByRole('button', { name: 'Remove report.txt', exact: true }).click()

  await expect(staged).toHaveCount(0)
  await expect(composer).toHaveValue(text)
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await expect.poll(() => socket.sent.length).toBe(1)
  const { client_submission_id: id, ...body } = submitted(socket)
  expect(body).toEqual({ content: text })
  accept(socket, id)
  await expect(page.locator('article').filter({ hasText: text }).getByRole('img', { name: 'Accepted', exact: true })).toBeVisible()
  expect(fixture.uploads).toEqual([])
  expect(socket.sent).toHaveLength(1)
})

test('an HTTP upload failure keeps the text and staged file for another Send', async ({ page }) => {
  const fixture = await prepareUploads(page)
  await page.route('**/api/chat/upload', (route) => {
    const request = route.request()
    expect(request.method()).toBe('POST')
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    expect(request.headers()['content-type']).toMatch(/^multipart\/form-data; boundary=/)
    return route.fulfill({ status: 503, json: { detail: 'Upload temporarily unavailable' } })
  }, { times: 1 })
  await openUploads(page, 'chat')
  const socket = await fixture.socket()
  const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
  const send = page.getByRole('button', { name: 'Send', exact: true })
  const text = 'Keep this draft.'
  await composer.fill(text)
  const input = page.getByLabel('Attach a file', { exact: true })
  await expect(input).toBeEnabled()
  await input.setInputFiles({ name: 'report.txt', mimeType: 'text/plain', buffer: Buffer.from('Report') })
  const staged = page.locator('form').getByText('report.txt', { exact: true })
  await expect(staged).toBeVisible()

  const failure = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat/upload' && response.status() === 503)
  await send.click()
  await failure

  await expect(page.locator('[data-sonner-toast]').filter({ hasText: 'Upload failed' })).toBeVisible()
  await expect(composer).toHaveValue(text)
  await expect(staged).toBeVisible()
  expect(socket.sent).toEqual([])
  await send.click()
  await expect.poll(() => socket.sent.length).toBe(1)
  expect(submitted(socket)).toMatchObject({ content: 'Keep this draft.\n\nAccepted upload', display_content: text,
    attachments: [{ path: 'uploads/accepted.txt', name: 'accepted.txt' }] })
  expect(fixture.uploads).toEqual([{ path: '/api/chat/upload' }])
  await expect(staged).toHaveCount(0)
})
