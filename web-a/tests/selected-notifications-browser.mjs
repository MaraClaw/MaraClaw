import assert from 'node:assert/strict'
import { captureNotifications, inspectNotifications, notificationFixture, visible } from './notifications-browser-evidence.mjs'

export async function exerciseSelectedMoves(page, { active, receipt, output, reload }) {
  const button = (label) => page.locator('main button').filter({ hasText: new RegExp(`^${label}$`, 'u') })
  const choose = (title) => page.locator(`input[aria-label="Select notification: ${title}"]`)
  const view = async (label) => { await page.snapshot(); await button(label).click() }
  const capture = (state) => captureNotifications(page, { active, receipt, output, state })
  for (const action of ['archive', 'restore']) {
    const label = action === 'archive' ? 'Archive selected' : 'Restore selected'
    const source = notificationFixture(action === 'restore').slice(0, 2)
    active.items = source
    await reload()
    if (action === 'restore') await view('Archived')
    await visible(choose(source[0].title))
    await page.snapshot(); await choose(source[0].title).click(); await choose(source[1].title).click()
    await capture(`${action}-selected`)
    // Given two mixed-read rows, when a batch fails, then both selections remain retryable.
    active.failSelected = 404
    await page.snapshot(); await button(`${label} \\(2\\)`).click()
    await visible(page.locator('[role="alert"]').filter({ hasText: 'Unable to move selected' }))
    assert.equal(await choose(source[0].title).evaluate((el) => el.checked), true)
    assert.equal(await choose(source[1].title).evaluate((el) => el.checked), true)
    await capture(`${action}-selected-error`)
    const before = receipt.requests.length
    // Given the retained selection, when retried from the keyboard, then one batch moves both unchanged read states.
    active.hold = { started: Promise.withResolvers(), released: Promise.withResolvers() }
    try {
      await page.snapshot(); await button(`${label} \\(2\\)`).focus(); await page.keyboard.press('Enter')
      await active.hold.started.promise
      assert.equal(await button('Mark selected as read \\(2\\)').evaluate((el) => el.disabled), true)
      assert.equal(await page.locator(`button[aria-label="${action === 'archive' ? 'Archive' : 'Restore'} notification: ${source[0].title}"]`).evaluate((el) => el.disabled), true)
      await capture(`${action}-selected-pending`)
      active.hold.released.resolve()
      await visible(page.locator('h1:focus'))
      assert.equal(await page.locator('main input[type="checkbox"]').count(), 0, 'moving the full source selection leaves its empty view')
      assert.deepEqual(receipt.requests.slice(before).filter((call) => call.method === 'POST').map(({ path, ids }) => ({ path, ids })), [{ path: `/api/notifications/${action}`, ids: source.map((item) => item.id) }])
      assert.deepEqual(active.items.map((item) => item.is_read), [false, true])
      assert.equal(await page.locator('nav[aria-label="Inbox pages"]').count(), 0, 'empty source traversal resets to its first page')
      await capture(`${action}-selected-complete`)
      await view(action === 'archive' ? 'Archived' : 'Active')
      await visible(choose(source[0].title))
      await inspectNotifications(page, { items: active.items, unread: action === 'archive' ? 0 : 1, receipt, active })
    } finally { active.hold.released.resolve(); active.hold = undefined }

    // Given a held move, when view navigation creates a new selection, then the old callback leaves it alone.
    active.items = [...notificationFixture(), ...notificationFixture(true)]
    await reload(); await visible(choose('Operations update'))
    if (action === 'restore') { await view('Archived'); await visible(choose('Saved: Operations update')) }
    const title = action === 'archive' ? 'Operations update' : 'Saved: Operations update'
    await page.snapshot(); await choose(title).click()
    active.hold = { started: Promise.withResolvers(), released: Promise.withResolvers() }
    try {
      await page.snapshot(); await button(`${label} \\(1\\)`).click(); await active.hold.started.promise
      await view(action === 'archive' ? 'Archived' : 'Active')
      const newer = choose(action === 'archive' ? 'Saved: Earlier team notice' : 'Earlier team notice')
      await visible(newer); await page.snapshot(); await newer.click(); await newer.focus()
      active.hold.released.resolve()
      await visible(button('Mark selected as read \\(1\\)'))
      assert.equal(await newer.evaluate((el) => el.checked), true)
      assert.equal(await newer.evaluate((el) => el === document.activeElement), true)
      await capture(`${action}-selected-new-view`)
    } finally { active.hold.released.resolve(); active.hold = undefined }

    for (const status of [200, 503]) {
      // Given a held move, when the session is replaced, then neither late success nor failure affects its new selection.
      active.items = [...notificationFixture(), ...notificationFixture(true)]
      await reload(); await visible(choose('Operations update'))
      if (action === 'restore') { await view('Archived'); await visible(choose(title)) }
      await page.snapshot(); await choose(title).click()
      active.hold = { started: Promise.withResolvers(), released: Promise.withResolvers() }
      if (status === 503) active.failSelected = 503
      try {
        await page.snapshot(); await button(`${label} \\(1\\)`).click(); await active.hold.started.promise
        await page.evaluate((token) => {
          localStorage.setItem('maraclaw-admin-token', token)
          window.dispatchEvent(new StorageEvent('storage', { key: 'maraclaw-admin-token', newValue: token, storageArea: localStorage }))
        }, `replacement-${action}-${status}`)
        await visible(button('Archive selected \\(0\\)'))
        const replacement = choose('Earlier team notice')
        await visible(replacement); await page.snapshot(); await replacement.click(); await replacement.focus()
        const beforeRelease = receipt.requests.length
        active.hold.released.resolve()
        await visible(button('Archive selected \\(1\\)'))
        assert.equal(await replacement.evaluate((el) => el.checked), true)
        assert.equal(await replacement.evaluate((el) => el === document.activeElement), true)
        assert.equal(await page.locator('main [role="alert"]').count(), 0)
        assert.equal(receipt.requests.slice(beforeRelease).length, 0)
        await capture(`${action}-selected-session-${status}`)
      } finally { active.hold.released.resolve(); active.hold = undefined }
    }
  }
}
