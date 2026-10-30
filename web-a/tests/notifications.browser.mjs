// Run: node tests/notifications.browser.mjs (existing Chromium + browser skill; no backend).
import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'vite'

import { notificationFixture } from './notifications-browser-evidence.mjs'
import { exerciseNotifications } from './notifications-browser-scenarios.mjs'

const matrix = [390, 1440, 1920].flatMap((width) => ['light', 'dark'].map((theme) => ({ width, theme })))
const admin = {
  id: '11111111-1111-4111-8111-111111111111', identity_id: '11111111-1111-4111-8111-111111111111',
  tenant_id: '22222222-2222-4222-8222-222222222222', role: 'org_admin', is_platform_admin: false,
  email: 'operator@example.test', display_name: 'Operator', avatar_url: null, title: null, primary_mobile: null,
  registration_source: null, is_active: true, email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}
await access(tmpdir())
const output = await mkdtemp(join(tmpdir(), 'web-a-notifications-'))
const receipt = { status: 'running', output, matrix, engine: 'omowright.connectPipe',
  brief: 'Review notifications with cursor Previous/Next, selected read, archive and restore using the existing design system.',
  layout: 'Layout: centered inbox card stack; shell main owns vertical scrolling; wrap selection and pagination controls on phones. No nested content scroller.',
  disclosure: 'Synthetic account and notifications; real Vite UI; backend proxy disabled.',
  scenarios: [], screenshots: [], requests: [], violations: [], cleanup: { tabsCreated: 0, tabsClosed: 0 } }
let browser, server, work, active

try {
  assert.equal(process.env.OMO_BROWSER_ENGINE, undefined, 'Owned browser refused when OMO_BROWSER_ENGINE is set; never bypass this restriction')
  const skill = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
  const { loadOmowright } = await import(pathToFileURL(join(skill, 'scripts/omowright.mjs')).href)
  const { omowright } = await loadOmowright()
  const executable = process.env.CHROMIUM_EXECUTABLE ?? (await import('@playwright/test')).chromium.executablePath()
  await access(executable)
  work = await mkdtemp(join(tmpdir(), 'notifications-owned-'))
  const profile = join(work, 'profile'); await mkdir(profile)
  server = await createServer({
    root: resolve(dirname(fileURLToPath(import.meta.url)), '..'), envDir: false, cacheDir: join(work, 'vite'),
    define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') }, server: { host: '127.0.0.1', port: 0 },
    plugins: [{ name: 'synthetic-notifications-firewall', enforce: 'pre',
      configResolved(config) { config.server.proxy = {}; receipt.proxyDisabled = true },
      configureServer(vite) { vite.middlewares.use(async (request, response, next) => {
        if (!request.url.startsWith('/api/')) return next()
        const url = new URL(request.url, 'http://synthetic.invalid'), path = url.pathname, method = request.method
        const call = { scenario: active?.name, path, method, query: url.search }
        receipt.requests.push(call)
        let body, status = 200
        if (method === 'GET' && path === '/api/auth/me') body = admin
        else if (active && method === 'GET' && path === '/api/notifications/unread-count') body = { unread_count: active.items.filter((item) => !item.is_read && item.archived_at === null).length }
        else if (active && method === 'GET' && path === '/api/notifications/page' && ['active', 'archived'].includes(url.searchParams.get('view')) && url.searchParams.get('limit') === '50') {
          if (active.listFailure) { status = 503; body = { detail: 'Unavailable' } }
          else if (active.pageOverride !== undefined) body = active.pageOverride
          else {
            assert.equal(url.searchParams.has('offset'), false)
            const archived = url.searchParams.get('view') === 'archived', cursor = url.searchParams.get('cursor')
            const rows = active.items.filter((item) => (item.archived_at !== null) === archived)
            const start = cursor === null ? 0 : rows.findIndex((item) => item.id === cursor) + 1
            const items = rows.slice(start, start + 50)
            body = { items, next_cursor: rows.length > start + 50 ? items.at(-1).id : null }
          }
        } else if (active && method === 'POST' && ['/api/notifications/read-selected', '/api/notifications/archive', '/api/notifications/restore'].includes(path)) {
          const owner = active, chunks = []
          for await (const chunk of request) chunks.push(chunk)
          const input = JSON.parse(Buffer.concat(chunks).toString())
          assert.ok(Array.isArray(input.ids) && input.ids.length > 0 && input.ids.length <= 200)
          call.ids = [...input.ids]
          const held = owner.hold
          if (held) { held.started.resolve(); await held.released.promise }
          if (owner.failSelected) { status = owner.failSelected; owner.failSelected = undefined; body = { detail: 'Unavailable' } }
          else {
            owner.items = owner.items.map((item) => {
              if (!input.ids.includes(item.id)) return item
              switch (path) {
                case '/api/notifications/read-selected': return { ...item, is_read: true }
                case '/api/notifications/archive': return { ...item, archived_at: item.archived_at ?? '2026-10-24T00:00:00Z' }
                case '/api/notifications/restore': return { ...item, archived_at: null }
                default: throw new Error(`Unsupported batch action ${path}`)
              }
            })
            body = { ok: true }
          }
        } else if (active && method === 'POST' && path === '/api/notifications/read-all') {
          active.items = active.items.map((item) => item.archived_at === null ? { ...item, is_read: true } : item); body = { ok: true }
        } else if (active && method === 'POST' && active.items.some((item) => ['read', 'archive', 'restore'].some((action) => path === `/api/notifications/${item.id}/${action}`))) {
          const action = path.split('/').at(-1)
          if (active.failNextAction === action) { active.failNextAction = undefined; status = 503; body = { detail: 'Unavailable' } }
          else {
            const held = active.hold
            if (held) { held.started.resolve(); await held.released.promise }
            active.items = active.items.map((item) => path === `/api/notifications/${item.id}/${action}`
              ? action === 'read' ? { ...item, is_read: true } : { ...item, archived_at: action === 'archive' ? item.archived_at ?? '2026-10-24T00:00:00Z' : null } : item)
            body = { ok: true }
          }
        } else { receipt.violations.push({ path, method }); status = 502; body = { detail: 'Unexpected synthetic API request' } }
        response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(body))
      }) },
    }],
  })
  await server.listen(); assert.equal(receipt.proxyDisabled, true)
  receipt.url = server.resolvedUrls.local[0]
  assert.notEqual(new URL(receipt.url).port, '4173')
  browser = await omowright.connectPipe({ browserPath: executable, storageRoot: profile, commandTimeoutMs: 15_000,
    browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--disable-component-update', '--disable-sync', `--user-data-dir=${profile}`], dialogPolicy: { accept: false } })
  for (const options of matrix) {
    active = { ...options, name: `${options.width}-${options.theme}`, items: [...notificationFixture(), ...notificationFixture(true)] }
    const viewport = { width: active.width, height: active.width === 390 ? 844 : 1000 }
    const manager = omowright.createAgentTabs(browser), tab = await manager.create('about:blank', { viewport })
    receipt.cleanup.tabsCreated++
    const page = tab.page, errors = []
    page.on('pageerror', (error) => errors.push(String(error)))
    try {
      await omowright.emulate(page, active.width === 390 ? 'iphone-14' : { ...viewport, deviceScaleFactor: 1, mobile: false, hasTouch: false })
      const session = await page.resolveSessionId()
      await page.cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: active.theme }, { name: 'prefers-reduced-motion', value: 'reduce' }] }, session)
      await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('maraclaw-admin-token','synthetic-notifications-token');localStorage.setItem('maraclaw-admin-theme',${JSON.stringify(active.theme)});` }, session)
      const reload = async () => {
        await page.goto(`${receipt.url}notifications`); await manager.repin(tab.targetId, viewport)
        if (active.width === 390) await omowright.emulate(page, 'iphone-14')
      }
      await reload()
      await exerciseNotifications(page, { active, receipt, output, reload })
      assert.deepEqual(errors, [], 'no uncaught page errors'); assert.deepEqual(receipt.violations, [], 'all API traffic stays synthetic')
      console.log(`PASS ${active.name}: active / archived / read / archive / restore / failure / pagination / empty`)
    } catch (error) {
      receipt.failureDOM = await page.evaluate(() => document.querySelector('main')?.innerText)
      throw error
    } finally { active.hold?.released.resolve(); await tab.close(); receipt.cleanup.tabsClosed++ }
  }
  receipt.status = 'passed'
} catch (error) {
  receipt.status = 'failed'; receipt.error = String(error); receipt.stack = error.stack; process.exitCode = 1
} finally {
  active?.hold?.released.resolve()
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
