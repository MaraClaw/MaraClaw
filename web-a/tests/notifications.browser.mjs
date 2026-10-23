// Run: node tests/notifications.browser.mjs (existing Chromium + browser skill; no backend).
import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'vite'

const matrix = [390, 1440, 1920].flatMap((width) => ['light', 'dark'].map((theme) => ({ width, theme })))
const admin = {
  id: '11111111-1111-4111-8111-111111111111', identity_id: '11111111-1111-4111-8111-111111111111',
  tenant_id: '22222222-2222-4222-8222-222222222222', role: 'org_admin', is_platform_admin: false,
  email: 'operator@example.test', display_name: 'Operator', avatar_url: null, title: null, primary_mobile: null,
  registration_source: null, is_active: true, email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}
const fixture = () => [false, true, false, true, true, true].map((is_read, index) => ({
  id: `33333333-3333-4333-8333-${String(index + 1).padStart(12, '0')}`, is_read, type: 'broadcast',
  title: ['Operations update', 'Earlier team notice', 'Workspace maintenance', 'Account review', 'Team summary', 'Previous update'][index],
  body: 'Review the latest workspace information with your team.\nUpdates remain available in your Inbox after they have been read.\nContact your organization administrator if you need assistance.',
  link: null, ref_id: null, sender_name: 'Workspace operations', created_at: '2026-10-20T12:00:00Z',
}))
await access(tmpdir())
const output = await mkdtemp(join(tmpdir(), 'web-a-notifications-'))
const receipt = { status: 'running', output, matrix, engine: 'omowright.connectPipe',
  disclosure: 'Synthetic account and notifications; real Vite UI; backend proxy disabled.',
  scenarios: [], screenshots: [], requests: [], violations: [], cleanup: { tabsCreated: 0, tabsClosed: 0 } }
let browser, server, work, active
const visible = (locator) => locator.waitFor({ state: 'visible', timeout: 15_000 })

async function inspect(page, expectedRead) {
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
    const tokens = {
      read: resolveColor('background-color', 'var(--card)'), unread: resolveColor('background-color', 'var(--accent)'),
      readBorder: resolveColor('border-color', 'var(--border)'),
      unreadBorder: resolveColor('border-color', 'color-mix(in oklab, var(--primary) 30%, transparent)'),
    }
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
        markButtons: card.querySelectorAll('button[aria-label^="Mark as read:"]').length }
    })
    return { tokens, cards, countLabels: [...document.querySelectorAll('a[href="/notifications"][aria-label]')].map((link) => link.getAttribute('aria-label')),
      countBadges: [...document.querySelectorAll('a[href="/notifications"] [data-slot="badge"]')].map((badge) => badge.textContent) }
  })
  assert.equal(rendered.cards.length, expectedRead.length, 'all notifications stay in the Inbox')
  assert.notDeepEqual(rendered.tokens.read, rendered.tokens.unread, 'theme provides distinct neutral/accent surfaces')
  for (const [index, read] of expectedRead.entries()) {
    const card = rendered.cards[index], input = fixture()[index]
    assert.deepEqual(card.background, read ? rendered.tokens.read : rendered.tokens.unread, `${input.title}: rendered background`)
    assert.deepEqual(card.border, read ? rendered.tokens.readBorder : rendered.tokens.unreadBorder, `${input.title}: rendered border`)
    assert.equal(card.badge, read ? 'Read' : 'Unread', 'existing textual badge remains')
    assert.equal(card.markButtons, read ? 0 : 1, 'only unread notifications offer Mark as read')
    assert.equal(card.title, input.title); assert.equal(card.body, input.body)
    assert.ok(card.contrast >= 4.5, `${input.title}: body contrast ${card.contrast.toFixed(2)}:1 >= 4.5:1`)
  }
  const count = expectedRead.filter((read) => !read).length
  assert.ok(rendered.countLabels.length > 0, 'navigation exposes an unread count')
  assert.ok(rendered.countLabels.every((label) => label === `Notifications, ${count} unread`), 'navigation count stays accurate')
  assert.deepEqual(rendered.countBadges, count ? rendered.countLabels.map(() => String(count)) : [], 'count badge disappears only at zero')
  receipt.scenarios.push({ scenario: active.name, read: expectedRead, ...rendered })
}

async function capture(page, state) {
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
    assert.ok(dimensions.scrollHeight > dimensions.clientHeight, 'fixture exercises the shell scroll owner')
    if (edge === 'scrolled-end') assert.ok(Math.abs(dimensions.top + dimensions.clientHeight - dimensions.scrollHeight) <= 1, 'scrolled to the actual end')
    if (active.width === 390) { assert.equal(dimensions.scale, 3); assert.ok(dimensions.touch > 0, 'true mobile emulation') }
    const session = await page.resolveSessionId()
    const metrics = await page.cdp.send('Page.getLayoutMetrics', {}, session)
    const shot = await page.cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true, captureBeyondViewport: true,
      ...(edge === 'fullpage' ? { clip: { x: 0, y: 0, width: metrics.cssContentSize.width, height: metrics.cssContentSize.height, scale: 1 } } : {}) }, session)
    const buffer = Buffer.from(shot.data, 'base64'), file = join(output, `${active.name}-${state}-${edge}.png`)
    assert.equal(buffer.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', 'capture is PNG')
    assert.equal(buffer.readUInt32BE(16), active.width * dimensions.scale, 'capture has requested physical width')
    await writeFile(file, buffer)
    receipt.screenshots.push({ file, state, edge, scenario: active.name, ...dimensions,
      pngWidth: buffer.readUInt32BE(16), pngHeight: buffer.readUInt32BE(20) })
  }
}

try {
  assert.equal(process.env.OMO_BROWSER_ENGINE, undefined, 'Owned browser refused when OMO_BROWSER_ENGINE is set; never bypass this restriction')
  const skill = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
  const { loadOmowright } = await import(pathToFileURL(join(skill, 'scripts/omowright.mjs')).href)
  const { omowright } = await loadOmowright()
  const executable = process.env.CHROMIUM_EXECUTABLE ?? (await import('@playwright/test')).chromium.executablePath()
  await access(executable) // Discover the existing browser only; never install or launch through Playwright.
  work = await mkdtemp(join(tmpdir(), 'notifications-owned-'))
  const profile = join(work, 'profile'); await mkdir(profile)
  server = await createServer({
    root: resolve(dirname(fileURLToPath(import.meta.url)), '..'), envDir: false, cacheDir: join(work, 'vite'),
    define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') }, server: { host: '127.0.0.1', port: 0 },
    plugins: [{ name: 'synthetic-notifications-firewall', enforce: 'pre',
      configResolved(config) { config.server.proxy = {}; receipt.proxyDisabled = true },
      configureServer(vite) { vite.middlewares.use((request, response, next) => {
        if (!request.url.startsWith('/api/')) return next()
        const url = new URL(request.url, 'http://synthetic.invalid'), path = url.pathname, method = request.method
        receipt.requests.push({ scenario: active?.name, path, method, query: url.search })
        let body, status = 200
        if (method === 'GET' && path === '/api/auth/me') body = admin
        else if (active && method === 'GET' && path === '/api/notifications/unread-count') body = { unread_count: active.items.filter((item) => !item.is_read).length }
        else if (active && method === 'GET' && path === '/api/notifications' && url.search === '?limit=50&offset=0') body = active.items
        else if (active && method === 'POST' && path === '/api/notifications/read-all') {
          active.items = active.items.map((item) => ({ ...item, is_read: true })); body = { ok: true }
        } else if (active && method === 'POST' && active.items.some((item) => path === `/api/notifications/${item.id}/read`)) {
          active.items = active.items.map((item) => path === `/api/notifications/${item.id}/read` ? { ...item, is_read: true } : item); body = { ok: true }
        } else { receipt.violations.push({ path, method }); status = 502; body = { detail: 'Unexpected synthetic API request' } }
        response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(body))
      }) },
    }],
  })
  await server.listen(); assert.equal(receipt.proxyDisabled, true)
  receipt.url = server.resolvedUrls.local[0]
  assert.notEqual(new URL(receipt.url).port, '4173', 'does not use the reserved member preview port')
  browser = await omowright.connectPipe({ browserPath: executable, storageRoot: profile, commandTimeoutMs: 15_000,
    browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--disable-component-update', '--disable-sync', `--user-data-dir=${profile}`], dialogPolicy: { accept: false } })
  for (const options of matrix) {
    active = { ...options, name: `${options.width}-${options.theme}`, items: fixture() }
    const viewport = { width: active.width, height: active.width === 390 ? 844 : 1000 }
    const navRegion = active.width === 390 ? 'header' : 'aside'
    const manager = omowright.createAgentTabs(browser), tab = await manager.create('about:blank', { viewport })
    receipt.cleanup.tabsCreated++
    const page = tab.page, errors = []
    page.on('pageerror', (error) => errors.push(String(error)))
    try {
      await omowright.emulate(page, active.width === 390 ? 'iphone-14' : { ...viewport, deviceScaleFactor: 1, mobile: false, hasTouch: false })
      const session = await page.resolveSessionId()
      await page.cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: active.theme }, { name: 'prefers-reduced-motion', value: 'reduce' }] }, session)
      await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('maraclaw-admin-token','synthetic-notifications-token');localStorage.setItem('maraclaw-admin-theme',${JSON.stringify(active.theme)});` }, session)
      // Given a mixed Inbox, when it loads, then real card colors and readable badges distinguish both states.
      await page.goto(`${receipt.url}notifications`); await manager.repin(tab.targetId, viewport)
      if (active.width === 390) await omowright.emulate(page, 'iphone-14')
      await visible(page.locator('ul[aria-label="Notifications"] li').last())
      await visible(page.locator(`${navRegion} a[aria-label="Notifications, 2 unread"]`))
      await inspect(page, [false, true, false, true, true, true]); await capture(page, 'mixed')
      // Given two unread items, when one is marked read, then only that item becomes neutral and the count becomes one.
      await page.snapshot()
      await page.locator('button[aria-label="Mark as read: Operations update"]').click()
      await page.locator('button[aria-label="Mark as read: Operations update"]').waitFor({ state: 'detached', timeout: 15_000 })
      await visible(page.locator(`${navRegion} a[aria-label="Notifications, 1 unread"]`))
      await inspect(page, [true, true, false, true, true, true]); await capture(page, 'one-read')
      // Given one remaining unread item, when all are marked read, then all cards are neutral and the count is zero.
      await page.snapshot()
      await page.locator('main button:not([aria-label]):not(:disabled)').filter({ hasText: /^Mark all as read$/u }).click()
      await page.locator('ul[aria-label="Notifications"] button[aria-label^="Mark as read:"]').waitFor({ state: 'detached', timeout: 15_000 })
      await visible(page.locator(`${navRegion} a[aria-label="Notifications, 0 unread"]`))
      await inspect(page, [true, true, true, true, true, true]); await capture(page, 'all-read')
      assert.equal(await page.locator('main button').filter({ hasText: /^Mark all as read$/u }).evaluate((button) => button.disabled), true)
      assert.deepEqual(errors, [], 'no uncaught page errors'); assert.deepEqual(receipt.violations, [], 'all API traffic stays synthetic')
      assert.deepEqual(receipt.requests.filter((call) => call.scenario === active.name && call.method === 'POST').map((call) => call.path),
        [`/api/notifications/${fixture()[0].id}/read`, '/api/notifications/read-all'], 'single and bulk use existing API actions exactly once')
      console.log(`PASS ${active.name}: mixed / one-read / all-read`)
    } finally { await tab.close(); receipt.cleanup.tabsClosed++ }
  }
  receipt.status = 'passed'
} catch (error) {
  receipt.status = 'failed'; receipt.error = String(error); process.exitCode = 1
} finally {
  try { if (browser) { await browser.close(); receipt.cleanup.browserClosed = true } }
  finally {
    try { if (server) { await server.close(); receipt.cleanup.viteClosed = true } }
    finally {
      if (work) { await rm(work, { recursive: true, force: true }); receipt.cleanup.profileAndCacheRemoved = true }
      receipt.finishedAt = new Date().toISOString()
      await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
      console.log(JSON.stringify({ status: receipt.status, output, url: receipt.url, scenarios: receipt.scenarios.length,
        screenshots: receipt.screenshots.length, cleanup: receipt.cleanup, error: receipt.error }))
    }
  }
}
