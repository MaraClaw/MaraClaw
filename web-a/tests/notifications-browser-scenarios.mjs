import assert from 'node:assert/strict'
import { notificationFixture, inspectNotifications, captureNotifications, visible } from './notifications-browser-evidence.mjs'

export async function exerciseNotifications(page, { active, receipt, output, reload }) {
  const original = notificationFixture(), saved = notificationFixture(true)
  const navRegion = active.width === 390 ? 'header' : 'aside'
  const button = (text) => page.locator('main button').filter({ hasText: new RegExp(`^${text}$`, 'u') })
  const count = (value) => visible(page.locator(`${navRegion} a[aria-label="Notifications, ${value} unread"]`))
  const inspect = (items, unread) => inspectNotifications(page, { items, unread, receipt, active })
  const capture = (state) => captureNotifications(page, { output, active, receipt, state })
  const view = async (text) => {
    await page.snapshot(); await button(text).click()
    await visible(page.locator('main button[aria-pressed="true"]').filter({ hasText: new RegExp(`^${text}$`, 'u') }))
  }
  await visible(page.locator('ul[aria-label="Notifications"] li').last()); await count(2)
  await inspect(original, 2); await capture('mixed')

  // Given an unread notification, when archive fails, then the row and active badge remain retryable.
  active.failNextAction = 'archive'
  await page.snapshot(); await page.locator('button[aria-label="Archive notification: Operations update"]').click()
  await visible(page.locator('[role="alert"]').filter({ hasText: 'Unable to archive' }))
  await inspect(original, 2); await capture('archive-error')

  // Given the same unread row, when archived by keyboard, then it moves without a read request.
  active.hold = { started: Promise.withResolvers(), released: Promise.withResolvers() }
  try {
    await page.snapshot(); await page.locator('button[aria-label="Archive notification: Operations update"]').focus()
    await page.keyboard.press('Enter'); await active.hold.started.promise
    assert.equal(await button('Archived').evaluate((element) => element.disabled), true)
    assert.equal(await page.locator('button[aria-label="Archive notification: Operations update"]').evaluate((element) => element.disabled), true)
    active.hold.released.resolve()
    await page.locator('button[aria-label="Archive notification: Operations update"]').waitFor({ state: 'detached', timeout: 15_000 })
    await count(1)
    await visible(page.locator('h1:focus'))
  } finally { active.hold.released.resolve(); active.hold = undefined }
  await inspect(original.slice(1), 1); await capture('archived-unread')
  await view('Archived'); await visible(page.locator('button[aria-label="Restore notification: Operations update"]'))
  await inspect([{ ...original[0], archived_at: '2026-10-24T00:00:00Z' }, ...saved], 1); await capture('archived-view')
  assert.equal(await button('Mark all as read').count(), 0, 'bulk read belongs only to Active')
  await page.snapshot(); await page.locator('button[aria-label="Restore notification: Operations update"]').focus(); await page.keyboard.press('Enter')
  await page.locator('button[aria-label="Restore notification: Operations update"]').waitFor({ state: 'detached', timeout: 15_000 }); await count(2)
  await inspect(saved, 2)

  // Given restored unread state, when explicitly read, then only that notification becomes read.
  await view('Active'); await visible(page.locator('button[aria-label="Mark as read: Operations update"]'))
  await page.snapshot(); await page.locator('button[aria-label="Mark as read: Operations update"]').click()
  await page.locator('button[aria-label="Mark as read: Operations update"]').waitFor({ state: 'detached', timeout: 15_000 }); await count(1)
  await inspect(original.map((item, index) => index === 0 ? { ...item, is_read: true } : item), 1); await capture('one-read')

  // Given cached archived unread rows, when Active is marked read, then Archived remains unread.
  await page.snapshot(); await button('Mark all as read').click()
  await page.locator('ul[aria-label="Notifications"] button[aria-label^="Mark as read:"]').waitFor({ state: 'detached', timeout: 15_000 }); await count(0)
  await inspect(original.map((item) => ({ ...item, is_read: true })), 0); await capture('all-read')
  assert.equal(await button('Mark all as read').evaluate((element) => element.disabled), true)
  await view('Archived'); await visible(page.locator('button[aria-label="Mark as read: Saved: Operations update"]'))
  await inspect(saved, 0); await capture('archived-after-read-all')
  await page.snapshot(); await page.locator('button[aria-label="Restore notification: Saved: Operations update"]').click()
  await page.locator('button[aria-label="Restore notification: Saved: Operations update"]').waitFor({ state: 'detached', timeout: 15_000 }); await count(1)
  await page.snapshot(); await page.locator('button[aria-label="Restore notification: Saved: Earlier team notice"]').click()
  await page.locator('button[aria-label="Restore notification: Saved: Earlier team notice"]').waitFor({ state: 'detached', timeout: 15_000 }); await count(1)
  await view('Active'); await visible(page.locator('button[aria-label="Archive notification: Saved: Earlier team notice"]'))
  await inspect([...original.map((item) => ({ ...item, is_read: true })), ...saved.slice(0, 2).map((item) => ({ ...item, archived_at: null }))], 1)
  await capture('restored-read-and-unread')
  await page.snapshot(); await page.locator('button[aria-label="Archive notification: Earlier team notice"]').click()
  await page.locator('button[aria-label="Archive notification: Earlier team notice"]').waitFor({ state: 'detached', timeout: 15_000 }); await count(1)
  assert.deepEqual(receipt.requests.filter((call) => call.scenario === active.name && call.method === 'POST' && call.path.endsWith('/read')).map((call) => call.path), [`/api/notifications/${original[0].id}/read`])

  // Given page two, when changing views in either direction, then pagination restarts at zero.
  active.items = Array.from({ length: 51 }, (_, index) => ({ ...original[1], id: `44444444-4444-4444-8444-${String(index + 1).padStart(12, '0')}`, title: `Paged notice ${index + 1}` })).concat(saved)
  await reload(); await visible(page.locator('button[aria-label="Archive notification: Paged notice 50"]'))
  await page.snapshot(); await button('Next').click(); await visible(page.locator('button[aria-label="Archive notification: Paged notice 51"]'))
  await view('Archived'); await visible(page.locator('button[aria-label="Restore notification: Saved: Operations update"]'))
  assert.ok(receipt.requests.some((call) => call.scenario === active.name && call.query === '?limit=50&offset=0&view=archived'))
  assert.ok(!receipt.requests.some((call) => call.scenario === active.name && call.query.includes('offset=50&view=archived')))
  await view('Active'); await visible(page.locator('button[aria-label="Archive notification: Paged notice 1"]'))
  active.listFailure = true
  await reload(); await visible(page.locator('[role="alert"]').filter({ hasText: 'Unable to load notifications' })); await capture('list-error')
  active.listFailure = false
  await page.snapshot(); await button('Retry').click(); await visible(page.locator('button[aria-label="Archive notification: Paged notice 1"]'))
  active.items = []
  await reload(); await visible(page.locator('h2').filter({ hasText: /^No active notifications$/u })); await capture('active-empty')
  await view('Archived'); await visible(page.locator('h2').filter({ hasText: /^No archived notifications$/u })); await capture('archived-empty')
}
