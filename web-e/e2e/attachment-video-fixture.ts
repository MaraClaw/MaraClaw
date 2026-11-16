import { expect, type Page } from '@playwright/test'
import type { AttachmentMetadata } from '../src/lib/attachment-preview-api'
import { chatPath, message, prepareChat } from './chat-identity-fixture'
import { originalToken } from './session-fencing-fixture'

// Silent, three-second VP8 WebM generated with ffmpeg's color source; no external media host.
export const videoBytes = Buffer.from('GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAMQEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHWTbuMU6uEElTDZ1OsggEzTbuMU6uEHFO7a1OsggL67AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsCrXsYMPQkBNgIxMYXZmNjMuMS4xMDJXQYxMYXZmNjMuMS4xMDJEiYhAp3AAAAAAABZUrmvYrgEAAAAAAABP14EBc8WIkp3s7SzDVJycgQAitZyDdW5kiIEAhoVWX1ZQOIOBASPjg4QdzWUA4JGwggFAuoG0moECVbCEVbmBAVXugQDsAQAAAAAAAAIAABJUw2f6c3OfY8CAZ8iZRaOHRU5DT0RFUkSHjExhdmY2My4xLjEwMnNz1WPAi2PFiJKd7O0sw1ScZ8igRaOHRU5DT0RFUkSHk0xhdmM2My4xLjEwMiBsaWJ2cHhnyKFFo4hEVVJBVElPTkSHkzAwOjAwOjAzLjAwMDAwMDAwMAAfQ7Z1QULngQCjQKGBAACA0A8AnQEqQAG0AABHCIWFiIWEiAICAnWqA/gD+AIGtqT3BoFkn2vbmyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eyc4eycuAP7upj/+8Z50Fzf2Lv/d5sAYS/4QAKOdgQH0ALECAAEQEAAYABhYL/QACIAEM1+tck+VgACjnYED6ACxAgABEBAAGAAYWC/0AAiABDNfrXJPlYAAo52BBdwAsQIAARAQABgAGFgv9AAIgAQzX61yT5WAAKOdgQfQALECAAEQEAAYABhYL/QACIAEM1+tck+VgACjnYEJxACxAgABEBAAGAAYWC/0AAiABDNfrXJPlYAAHFO7a5G7j7OBALeK94EB8YIBsvCBAw==', 'base64')
export const videoAttachment = { path: '.maraclaw/attachments/telegram/opaque/clip.webm', name: 'Channel clip.webm' }
export const videoMessage = { ...message('im-video', 'Clip from Telegram', 'user'), attachments: [videoAttachment] }

export async function prepareVideoChat(page: Page, overrides: Partial<AttachmentMetadata> = {}) {
  const chat = await prepareChat(page, [videoMessage])
  let metadataCalls = 0
  let byteCalls = 0
  await page.addInitScript(() => {
    const revoke = URL.revokeObjectURL.bind(URL)
    URL.revokeObjectURL = (url) => {
      document.documentElement.dataset.revokedPreviewUrl = url
      revoke(url)
    }
  })
  await page.route('**/files/preview-*?*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    expect(request.method()).toBe('GET')
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    expect(url.searchParams.get('path')).toBe(videoAttachment.path)
    expect(url.searchParams.has('token')).toBe(false)
    if (url.pathname.endsWith('/preview-metadata')) {
      metadataCalls++
      return route.fulfill({ json: {
        ...videoAttachment, size: videoBytes.length, version_token: 'video-v1', content_type: 'video/webm', ...overrides,
      } })
    }
    expect(url.searchParams.get('version_token')).toBe('video-v1')
    byteCalls++
    return route.fulfill({ contentType: 'application/octet-stream', body: videoBytes })
  })
  return { chat, metadataCalls: () => metadataCalls, byteCalls: () => byteCalls }
}

export async function openVideoChat(page: Page, overrides: Partial<AttachmentMetadata> = {}) {
  const fixture = await prepareVideoChat(page, overrides)
  await page.goto(chatPath())
  await fixture.chat.socket()
  expect(fixture.metadataCalls()).toBe(0)
  expect(fixture.byteCalls()).toBe(0)
  await page.getByRole('button', { name: 'Preview', exact: true }).click()
  return fixture
}
