import assert from 'node:assert/strict'
import { notificationFixture, inspectNotifications, captureNotifications, visible } from './notifications-browser-evidence.mjs'
import { exerciseSelectedMoves } from './selected-notifications-browser.mjs'

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
    await visible(page.locator('button[aria-label="Archive notification: Operations update"][disabled]'))
    assert.equal(await button('Archived').evaluate((element) => element.disabled), false)
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

  // Given page two, when changing views in either direction, then pagination restarts without a cursor.
  active.items = Array.from({ length: 51 }, (_, index) => ({ ...original[1], id: `44444444-4444-4444-8444-${String(index + 1).padStart(12, '0')}`, title: `Paged notice ${index + 1}` })).concat(saved)
  await reload(); await visible(page.locator('button[aria-label="Archive notification: Paged notice 50"]'))

  // Given selected rows, when the batch fails, then selection remains available for retry.
  const choose = (title) => page.locator(`input[aria-label="Select notification: ${title}"]`)
  await page.snapshot(); await choose('Paged notice 1').focus()
  const session = await page.resolveSessionId()
  for (const type of ['keyDown', 'keyUp']) await page.cdp.send('Input.dispatchKeyEvent', { type, key: ' ', code: 'Space', windowsVirtualKeyCode: 32 }, session)
  await visible(button('Mark selected as read \\(1\\)'))
  active.failSelected = 503
  await page.snapshot(); await button('Mark selected as read \\(1\\)').click()
  await visible(page.locator('[role="alert"]').filter({ hasText: 'Unable to mark selected' }))
  assert.equal(await choose('Paged notice 1').evaluate((element) => element.checked), true)
  await capture('selected-error')

  // Given a held page snapshot, when navigating and selecting another view, then completion preserves replacement selection.
  await page.snapshot(); await page.locator('main input[type="checkbox"]').first().click()
  await visible(button('Mark selected as read \\(50\\)')); await capture('selected-page')
  active.hold = { started: Promise.withResolvers(), released: Promise.withResolvers() }
  try {
    await page.snapshot(); await button('Mark selected as read \\(50\\)').click(); await active.hold.started.promise
    const submitted = receipt.requests.filter((call) => call.scenario === active.name && call.path.endsWith('/read-selected')).at(-1)
    assert.equal(submitted.ids.length, 50)
    assert.deepEqual(submitted.ids, active.items.slice(0, 50).map((item) => item.id))
    await capture('selected-pending')
    await page.snapshot(); await button('Next').click(); await visible(choose('Paged notice 51'))
    assert.equal(await choose('Paged notice 51').evaluate((element) => element.checked), false)
    await page.snapshot(); await choose('Paged notice 51').click()
    await view('Archived'); await visible(choose('Saved: Operations update'))
    assert.equal(await choose('Saved: Operations update').evaluate((element) => element.checked), false)
    await page.snapshot(); await choose('Saved: Operations update').click()
    active.hold.released.resolve()
    await visible(button('Mark selected as read \\(1\\)'))
    assert.equal(await choose('Saved: Operations update').evaluate((element) => element.checked), true)
    assert.equal(receipt.requests.filter((call) => call.scenario === active.name && call.path.endsWith('/read-selected')).length, 2)
    await capture('selected-replacement')
  } finally { active.hold.released.resolve(); active.hold = undefined }
  await page.snapshot(); await button('Mark selected as read \\(1\\)').click()
  await visible(button('Mark selected as read \\(0\\)')); await count(0)
  await capture('selected-archived-read')

  await view('Active'); await visible(choose('Paged notice 1'))
  await page.snapshot(); await choose('Paged notice 1').click()
  active.hold = { started: Promise.withResolvers(), released: Promise.withResolvers() }
  active.failSelected = 503
  try {
    await page.snapshot(); await button('Mark selected as read \\(1\\)').click(); await active.hold.started.promise
    await page.snapshot(); await button('Next').click(); await visible(choose('Paged notice 51'))
    await page.snapshot(); await choose('Paged notice 51').click()
    await view('Archived'); await visible(choose('Saved: Operations update'))
    await page.snapshot(); await choose('Saved: Operations update').click()
    active.hold.released.resolve()
    await visible(button('Mark selected as read \\(1\\)'))
    assert.equal(await page.locator('main [role="alert"]').count(), 0)
    assert.equal(await choose('Saved: Operations update').evaluate((element) => element.checked), true)
    await capture('selected-navigation-failure')
  } finally { active.hold.released.resolve(); active.hold = undefined }

  // Given a held batch, when a storage event replaces the admin session, then its completion cannot clear new selection.
  await view('Active'); await visible(choose('Paged notice 1'))
  await page.snapshot(); await choose('Paged notice 1').click()
  active.hold = { started: Promise.withResolvers(), released: Promise.withResolvers() }
  try {
    await page.snapshot(); await button('Mark selected as read \\(1\\)').click(); await active.hold.started.promise
    await page.evaluate(() => {
      localStorage.setItem('maraclaw-admin-token', 'replacement-synthetic-notifications-token')
      window.dispatchEvent(new StorageEvent('storage', { key: 'maraclaw-admin-token', newValue: 'replacement-synthetic-notifications-token', storageArea: localStorage }))
    })
    await visible(button('Mark selected as read \\(0\\)'))
    await page.snapshot(); await choose('Paged notice 2').click()
    active.hold.released.resolve()
    await visible(button('Mark selected as read \\(1\\)'))
    assert.equal(await choose('Paged notice 2').evaluate((element) => element.checked), true)
    await capture('selected-session-replacement')
  } finally { active.hold.released.resolve(); active.hold = undefined }

  await page.snapshot(); await button('Next').click(); await visible(page.locator('button[aria-label="Archive notification: Paged notice 51"]'))
  // Given a continuation, inserting a new head row must not shift its boundary or duplicate the previous last row.
  await page.snapshot(); await button('Previous').click(); await visible(choose('Paged notice 1'))
  const inserted = { ...original[1], id: '55555555-5555-4555-8555-555555555555', title: 'New arrival' }
  active.items = [inserted, ...active.items]
  await page.snapshot(); await button('Next').click(); await visible(choose('Paged notice 51'))
  assert.equal(await choose('Paged notice 50').count(), 0)
  assert.equal(await choose('New arrival').count(), 0)
  assert.equal(await button('Next').evaluate((element) => element.disabled), true, 'short final page ends traversal')
  await capture('cursor-final')

  // Given a later page, a successful mutation restarts from the new live head.
  await page.snapshot(); await page.locator('button[aria-label="Archive notification: Paged notice 51"]').click()
  await visible(choose('New arrival'))
  assert.equal(await button('Previous').evaluate((element) => element.disabled), true)
  await capture('cursor-mutation-reset')
  active.items = active.items.filter((item) => item.id !== inserted.id).map((item) => item.title === 'Paged notice 51' ? { ...item, archived_at: null } : item)

  // Given a duplicate row in an envelope, visible controls and selection contain it only once.
  active.pageOverride = { items: [original[1], original[1]], next_cursor: 'empty-final' }
  await reload(); await visible(choose('Earlier team notice'))
  assert.equal(await choose('Earlier team notice').count(), 1)
  active.pageOverride = { items: [], next_cursor: null }
  await page.snapshot(); await button('Next').click(); await visible(page.locator('h2').filter({ hasText: /^No more notifications$/u }))
  assert.equal(await button('Next').evaluate((element) => element.disabled), true)
  await capture('cursor-empty-final')
  active.pageOverride = { items: [], next_cursor: 7 }
  await reload(); await visible(page.locator('[role="alert"]').filter({ hasText: 'Unable to load notifications' }))
  await capture('cursor-malformed')
  active.pageOverride = undefined
  await page.snapshot(); await button('Retry').click(); await visible(choose('Paged notice 1'))

  await view('Archived'); await visible(page.locator('button[aria-label="Restore notification: Saved: Operations update"]'))
  assert.ok(receipt.requests.some((call) => call.scenario === active.name && call.query === '?limit=50&view=archived'))
  assert.ok(!receipt.requests.some((call) => call.scenario === active.name && call.query.includes('view=archived&cursor=')))
  await view('Active'); await visible(page.locator('button[aria-label="Archive notification: Paged notice 1"]'))
  active.listFailure = true
  await reload(); await visible(page.locator('[role="alert"]').filter({ hasText: 'Unable to load notifications' })); await capture('list-error')
  active.listFailure = false
  await page.snapshot(); await button('Retry').click(); await visible(page.locator('button[aria-label="Archive notification: Paged notice 1"]'))
  await exerciseSelectedMoves(page, { active, receipt, output, reload })
  active.items = []
  await reload(); await visible(page.locator('h2').filter({ hasText: /^No active notifications$/u })); await capture('active-empty')
  await view('Archived'); await visible(page.locator('h2').filter({ hasText: /^No archived notifications$/u })); await capture('archived-empty')
}
