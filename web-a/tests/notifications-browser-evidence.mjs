import assert from 'node:assert/strict'
import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'

import { NotificationKind } from '../src/lib/notifications-api.ts'

export const notificationFixture = (archived = false) => [false, true, false, true, true, true].map((is_read, index) => ({
  id: `33333333-3333-4333-8333-${String(index + (archived ? 101 : 1)).padStart(12, '0')}`, is_read, type: NotificationKind.Broadcast,
  title: `${archived ? 'Saved: ' : ''}${['Operations update', 'Earlier team notice', 'Workspace maintenance', 'Account review', 'Team summary', 'Previous update'][index]}`,
  body: 'Review the latest workspace information with your team.\nUpdates remain available in your Inbox after they have been read.\nContact your organization administrator if you need assistance.',
  link: null, ref_id: null, sender_name: 'Workspace operations', created_at: '2026-10-20T12:00:00Z',
  archived_at: archived ? '2026-10-23T12:00:00Z' : null,
}))

export const visible = (locator) => locator.waitFor({ state: 'visible', timeout: 15_000 })

export async function inspectNotifications(page, { items, unread, receipt, active }) {
  const rendered = await page.evaluate(() => {
    const canvas = document.createElement('canvas'), context = canvas.getContext('2d')
    const rgba = (css) => {
      context.clearRect(0, 0, 1, 1); context.fillStyle = css; context.fillRect(0, 0, 1, 1)
      return [...context.getImageData(0, 0, 1, 1).data]
    }
    const probe = document.createElement('span'); document.body.append(probe)
    const resolveColor = (property, value) => {
      probe.style.setProperty(property, value)
      return rgba(getComputedStyle(probe).getPropertyValue(property))
    }
    const tokens = { read: resolveColor('background-color', 'var(--card)'), unread: resolveColor('background-color', 'var(--accent)'), border: resolveColor('border-color', 'var(--border)') }
    probe.remove()
    const luminance = (color) => color.slice(0, 3).map((byte) => byte / 255)
      .map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
      .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0)
    const cards = [...document.querySelectorAll('ul[aria-label="Notifications"] [data-slot="card"]')].map((card) => {
      const css = getComputedStyle(card), body = card.querySelector('p'), text = getComputedStyle(body)
      const background = rgba(css.backgroundColor), foreground = rgba(text.color)
      const a = luminance(background), b = luminance(foreground)
      return { background, border: rgba(css.borderTopColor), badge: card.querySelector('[data-slot="badge"]').textContent,
        title: card.querySelector('h2').textContent, body: body.textContent, contrast: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05),
        markButtons: card.querySelectorAll('button[aria-label^="Mark as read:"]').length,
        archiveButtons: card.querySelectorAll('button[aria-label^="Archive notification:"]').length,
        restoreButtons: card.querySelectorAll('button[aria-label^="Restore notification:"]').length }
    })
    return { tokens, cards, countLabels: [...document.querySelectorAll('a[href="/notifications"][aria-label]')].map((link) => link.getAttribute('aria-label')),
      countBadges: [...document.querySelectorAll('a[href="/notifications"] [data-slot="badge"]')].map((badge) => badge.textContent) }
  })
  assert.equal(rendered.cards.length, items.length, 'only the selected view is rendered')
  assert.notDeepEqual(rendered.tokens.read, rendered.tokens.unread, 'theme provides distinct neutral/accent surfaces')
  for (const [index, input] of items.entries()) {
    const card = rendered.cards[index]
    assert.deepEqual(card.background, input.is_read ? rendered.tokens.read : rendered.tokens.unread, `${input.title}: rendered background`)
    assert.deepEqual(card.border, rendered.tokens.border, `${input.title}: neutral border`)
    assert.equal(card.badge, input.is_read ? 'Read' : 'Unread')
    assert.equal(card.markButtons, input.is_read ? 0 : 1)
    assert.equal(card.archiveButtons, input.archived_at === null ? 1 : 0)
    assert.equal(card.restoreButtons, input.archived_at === null ? 0 : 1)
    assert.equal(card.title, input.title); assert.equal(card.body, input.body)
    assert.ok(card.contrast >= 4.5, `${input.title}: body contrast ${card.contrast.toFixed(2)}:1 >= 4.5:1`)
  }
  assert.ok(rendered.countLabels.length > 0)
  assert.ok(rendered.countLabels.every((label) => label === `Notifications, ${unread} unread`))
  assert.deepEqual(rendered.countBadges, unread ? rendered.countLabels.map(() => String(unread)) : [])
  receipt.scenarios.push({ scenario: active.name, unread, ...rendered })
}

export async function captureNotifications(page, { output, active, receipt, state }) {
  await page.snapshot()
  for (const edge of ['fullpage', 'scrolled-end']) {
    await page.evaluate(async (end) => {
      document.querySelector('main').scrollTop = end ? document.querySelector('main').scrollHeight : 0
      window.scrollTo(0, end ? document.documentElement.scrollHeight : 0)
      await document.fonts.ready
      await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))
    }, edge === 'scrolled-end')
    const dimensions = await page.evaluate(() => {
      const main = document.querySelector('main')
      return { width: innerWidth, height: innerHeight, scale: devicePixelRatio, touch: navigator.maxTouchPoints,
        dark: document.documentElement.classList.contains('dark'), documentFits: document.documentElement.scrollWidth <= innerWidth,
        mainFits: main.scrollWidth <= main.clientWidth, top: main.scrollTop, scrollHeight: main.scrollHeight, clientHeight: main.clientHeight }
    })
    assert.equal(dimensions.width, active.width); assert.equal(dimensions.dark, active.theme === 'dark')
    assert.ok(dimensions.documentFits && dimensions.mainFits, 'no horizontal overflow')
    if (edge === 'scrolled-end') assert.ok(Math.abs(dimensions.top + dimensions.clientHeight - dimensions.scrollHeight) <= 1, 'scrolled to the actual end')
    if (active.width === 390) { assert.equal(dimensions.scale, 3); assert.ok(dimensions.touch > 0, 'true mobile emulation') }
    const session = await page.resolveSessionId()
    const metrics = await page.cdp.send('Page.getLayoutMetrics', {}, session)
    const shot = await page.cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: true,
      ...(edge === 'fullpage' ? { clip: { x: 0, y: 0, width: metrics.cssContentSize.width, height: metrics.cssContentSize.height, scale: 1 } } : {}) }, session)
    const buffer = Buffer.from(shot.data, 'base64'), file = join(output, `${active.name}-${state}-${edge}.png`)
    assert.equal(buffer.subarray(0, 8).toString('hex'), '89504e470d0a1a0a')
    assert.equal(buffer.readUInt32BE(16), active.width * dimensions.scale)
    await writeFile(file, buffer)
    receipt.screenshots.push({ file, state, edge, scenario: active.name, ...dimensions,
      pngWidth: buffer.readUInt32BE(16), pngHeight: buffer.readUInt32BE(20) })
  }
}
