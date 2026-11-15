import assert from 'node:assert/strict'
import { appendFileSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { chromium, devices, expect } from '@playwright/test'
import { createServer } from 'vite'

export const company = {
  id: '11111111-1111-4111-8111-111111111111', name: 'Synthetic Company', slug: 'synthetic-company',
  is_active: false, sso_enabled: false, sso_domain: null, is_system: false,
  is_default_end_user_org: false, can_disable: true, created_at: null,
  user_count: 7, active_user_count: 0, inactive_user_count: 7, agent_count: 3,
  org_admin_email: 'admin@example.test', phone: null, tier: 'max',
}
const admin = {
  id: '22222222-2222-4222-8222-222222222222', identity_id: '33333333-3333-4333-8333-333333333333',
  email: 'operator@example.test', display_name: 'Operator', avatar_url: null,
  role: 'platform_admin', is_platform_admin: true, tenant_id: company.id,
  is_active: true, email_verified: true, must_change_password: false, created_at: '2026-11-01T00:00:00Z',
}

export async function companyHarness() {
  assert.equal(Boolean(process.env.OMO_BROWSER_ENGINE), false, 'Company QA requires an owned synthetic browser')
  const output = process.env.COMPANY_OPERATIONS_ARTIFACTS
  assert.ok(output, 'COMPANY_OPERATIONS_ARTIFACTS is required')
  await mkdir(output, { recursive: true })
  const receipt = { screenshots: [], snapshots: [], traces: [], scenarios: [], requests: [], layouts: [], resources: { browsers: [] }, cleanup: {} }
  const resources = new AsyncDisposableStack()
  async function close() {
    try { await resources.disposeAsync() }
    finally {
      receipt.cleanup.browserClosed = receipt.resources.browsers.every(browser => browser.browserClosed)
      receipt.cleanup.profileRemoved = receipt.resources.browsers.every(browser => browser.profileRemoved)
      await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
    }
  }
  try {
  const server = await createServer({ configLoader: 'runner', cacheDir: join(process.env.TMPDIR, 'vite-cache'), server: { host: '127.0.0.1', port: 0, hmr: false }, plugins: [{
    name: 'company-no-live-api', configureServer(instance) {
      instance.middlewares.use((request, response, next) => {
        if (!(request.url ?? '').startsWith('/api')) return next()
        response.writeHead(599)
        response.end('Synthetic API interception required')
      })
    },
  }] })
  resources.defer(async () => {
    await server.close()
    receipt.cleanup.viteClosed = true
  })
  await server.listen()
  const base = server.resolvedUrls.local[0]
  receipt.isolation = []
  for (const path of ['/api', '/api?company-qa=1', '/api/_company-qa']) {
    const response = await fetch(new URL(path, base), { signal: AbortSignal.timeout(15_000) })
    assert.equal(response.status, 599, `${path} must stop before the configured Vite proxy`)
    assert.equal(await response.text(), 'Synthetic API interception required')
    receipt.isolation.push({ path, status: response.status })
  }
  const root = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
  const { loadOmowright } = await import(pathToFileURL(join(root, 'scripts/omowright.mjs')).href)
  const { omowright } = await loadOmowright()
  receipt.resources.vitePort = server.httpServer.address().port
  async function scenario(name, options, exercise) {
    console.log(`START ${name}`)
    {
    await using scope = new AsyncDisposableStack()
    const profile = await mkdtemp(join(tmpdir(), 'company-operations-owned-'))
    const resource = { name, profile, profileRemoved: false, browserClosed: false, contextClosed: false }
    receipt.resources.browsers.push(resource)
    scope.defer(async () => {
      await rm(profile, { recursive: true, force: true })
      resource.profileRemoved = true
    })
    const owned = await omowright.connectPipe({
      browserPath: process.env.CHROMIUM_EXECUTABLE ?? chromium.executablePath(),
      browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${profile}`],
      storageRoot: profile, commandTimeoutMs: 15_000, readinessTimeoutMs: 15_000,
    })
    scope.defer(async () => {
      await owned.close()
      resource.browserClosed = true
    })
    resource.browserPid = owned.browserProcess.pid
    owned.browserProcess.stderr.on('data', chunk => appendFileSync(join(output, `${name}.stderr.log`), chunk))
    owned.browserProcess.on('exit', (code, signal) => { resource.exit = { code, signal } })
    const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
    const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, { timeout: 15_000 })
    scope.defer(() => browser.close())
    const context = await browser.newContext({ ...(options.mobile ? devices['iPhone 14'] : { viewport: { width: options.width ?? 1440, height: 1000 } }), colorScheme: options.theme, reducedMotion: 'reduce' })
    scope.defer(async () => {
      await context.close()
      resource.contextClosed = true
    })
    context.setDefaultTimeout(15_000)
    context.setDefaultNavigationTimeout(15_000)
    const state = { company: { ...company }, handle: null }
    const errors = []
      await context.tracing.start({ screenshots: true, snapshots: true })
      scope.defer(async () => {
        const path = join(output, `${name}.trace.zip`)
        await context.tracing.stop({ path })
        receipt.traces.push(path)
      })
      await context.addInitScript((theme) => {
        localStorage.setItem('maraclaw-admin-token', 'synthetic-company-token')
        localStorage.setItem('maraclaw-admin-theme', theme)
      }, options.theme)
      await context.route('**/*', (route) => new URL(route.request().url()).origin === new URL(base).origin ? route.continue() : route.abort())
      const page = await context.newPage()
      page.on('pageerror', error => errors.push(error.message))
      await page.route('**/api/**', async route => {
        const url = new URL(route.request().url())
        const path = url.pathname
        receipt.requests.push({ name, method: route.request().method(), path, query: Object.fromEntries(url.searchParams), body: route.request().postDataJSON() })
        if (state.handle && await state.handle(route, url)) return
        let body
        if (path === '/api/auth/me') body = admin
        else if (path === '/api/notifications/unread-count') body = { unread_count: 0 }
        else if (path === '/api/admin/companies/page') body = { items: [state.company], next_cursor: null }
        else if (path.endsWith('/stats')) body = state.company
        else if (path.endsWith('/email-domains')) body = []
        else if (path.endsWith('/agent-sections')) body = { sections: ['chat', 'files'], hidden_sections: [] }
        else if (path === '/api/organization-plans') body = { plans: [] }
        else throw new Error(`Unexpected synthetic API ${path}`)
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
      })
      await exercise(page, state, base)
      assert.deepEqual(errors, [])
      const target = (await owned.listTargets()).find(target => target.type === 'page' && target.url === page.url())
      assert.ok(target, 'The scenario page must belong to the owned browser')
      const livePage = await owned.attachPage(target.id)
      const path = join(output, `${name}.snapshot.json`)
      await writeFile(path, JSON.stringify(await livePage.snapshot(), null, 2))
      receipt.snapshots.push(path)
    }
    receipt.scenarios.push(name)
    console.log(`PASS ${name}`)
  }
  async function capture(page, name) {
    await expect(page.getByRole('heading', { name: 'Synthetic Company', exact: true })).toBeVisible()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    const layout = await page.locator('main').evaluate(main => ({ height: main.clientHeight, contentHeight: main.scrollHeight, scrollTop: main.scrollTop, overflow: getComputedStyle(main).overflowY }))
    assert.ok(layout.height > 0, 'The scroll viewport must have positive height')
    receipt.layouts.push({ name, ...layout })
    await page.locator('main').evaluate(main => { main.scrollTop = 0 })
    const path = join(output, `${name}.png`)
    await page.screenshot({ path, fullPage: true, animations: 'disabled' })
    receipt.screenshots.push(path)
    for (let offset = layout.height; offset < layout.contentHeight; offset += layout.height) {
      await page.locator('main').evaluate((main, top) => { main.scrollTop = top }, offset)
      const scrolled = join(output, `${name}-scroll-${offset}.png`)
      await page.screenshot({ path: scrolled, animations: 'disabled' })
      receipt.screenshots.push(scrolled)
    }
    await page.locator('main').evaluate((main, top) => { main.scrollTop = top }, layout.scrollTop)
  }
  return { scenario, capture, receipt, output, close }
  } catch (error) {
    await close()
    throw error
  }
}
