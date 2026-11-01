import assert from 'node:assert/strict'
import { captureNotifications, inspectNotifications, notificationFixture, visible } from './notifications-browser-evidence.mjs'

export async function exerciseSelectedUnread(page, { active, receipt, output, reload }) {
  const button = (label) => page.locator('main button').filter({ hasText: new RegExp(`^${label}$`, 'u') })
  const choose = (title) => page.locator(`input[aria-label="Select notification: ${title}"]`)
  const capture = (state) => captureNotifications(page, { active, receipt, output, state })
  for (const archived of [false, true]) {
    const source = notificationFixture(archived).slice(0, 2)
    active.items = source
    await reload()
    if (archived) { await page.snapshot(); await button('Archived').click() }
    await visible(choose(source[0].title))
    await page.snapshot(); await choose(source[0].title).click(); await choose(source[1].title).click()
    active.failSelected = 404
    await page.snapshot(); await button('Mark selected as unread \\(2\\)').click()
    await visible(page.locator('[role="alert"]').filter({ hasText: 'Unable to mark selected notifications as unread' }))
    assert.equal(await choose(source[1].title).evaluate((el) => el.checked), true)
    assert.deepEqual(active.items, source)
    await capture(`unread-${archived ? 'archived' : 'active'}-error`)
    const before = receipt.requests.length
    active.hold = { started: Promise.withResolvers(), released: Promise.withResolvers() }
    try {
      await page.snapshot(); await button('Mark selected as unread \\(2\\)').focus(); await page.keyboard.press('Enter')
      await active.hold.started.promise
      assert.equal(await button('Mark selected as read \\(2\\)').evaluate((el) => el.disabled), true)
      assert.equal(await button(`${archived ? 'Restore' : 'Archive'} selected \\(2\\)`).evaluate((el) => el.disabled), true)
      await capture(`unread-${archived ? 'archived' : 'active'}-pending`)
      active.hold.released.resolve()
      await visible(page.locator('h1:focus'))
      await visible(button('Mark selected as unread \\(0\\)'))
      assert.deepEqual(receipt.requests.slice(before).filter((call) => call.method === 'POST').map(({ path, ids }) => ({ path, ids })), [{ path: '/api/notifications/unread-selected', ids: source.map((row) => row.id) }])
      assert.deepEqual(active.items, source.map((row) => ({ ...row, is_read: false })))
      await inspectNotifications(page, { items: active.items, unread: archived ? 0 : 2, receipt, active })
      await capture(`unread-${archived ? 'archived' : 'active'}-complete`)
    } finally { active.hold.released.resolve(); active.hold = undefined }
    await reload()
    if (archived) { await page.snapshot(); await button('Archived').click() }
    await visible(choose(source[0].title))
    await inspectNotifications(page, { items: active.items, unread: archived ? 0 : 2, receipt, active })
  }

  for (const status of [200, 503]) {
    active.items = notificationFixture().map((row) => ({ ...row, is_read: true }))
    await reload(); await visible(choose('Operations update'))
    await page.snapshot(); await choose('Operations update').click()
    active.hold = { started: Promise.withResolvers(), released: Promise.withResolvers(), finished: Promise.withResolvers() }
    if (status === 503) active.failSelected = 503
    try {
      await page.snapshot(); await button('Mark selected as unread \\(1\\)').click(); await active.hold.started.promise
      await page.evaluate((token) => {
        localStorage.setItem('maraclaw-admin-token', token)
        window.dispatchEvent(new StorageEvent('storage', { key: 'maraclaw-admin-token', newValue: token, storageArea: localStorage }))
      }, `replacement-unread-${status}`)
      await visible(button('Mark selected as unread \\(0\\)'))
      const newer = choose('Earlier team notice')
      await page.snapshot(); await newer.click(); await newer.focus()
      const beforeRelease = receipt.requests.length
      active.hold.released.resolve(); await active.hold.finished.promise
      await page.snapshot()
      assert.equal(await newer.evaluate((el) => el.checked && el === document.activeElement), true)
      assert.equal(await page.locator('main [role="alert"]').count(), 0)
      assert.equal(receipt.requests.length, beforeRelease)
      await capture(`unread-session-${status}`)
    } finally { active.hold.released.resolve(); active.hold = undefined }
  }
}
