import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { chromium, devices, expect as playwrightExpect } from '@playwright/test'
import { build, preview } from 'vite'

export const expect = playwrightExpect.configure({ timeout: 15_000 })
export const companyId = 'a8122022-c233-4f10-91df-91e2d065d2cb'
export const otherCompanyId = '66dccce3-bcfb-40ef-b334-49c2b47d614d'
const admin = {
  id: otherCompanyId, identity_id: otherCompanyId, email: 'analytics@example.test', display_name: 'Analytics operator',
  avatar_url: null, role: 'platform_admin', is_platform_admin: true, tenant_id: companyId, title: null,
  primary_mobile: null, registration_source: null, is_active: true, email_verified: true,
  must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}
export function summary(count = 100, scope = 'system') {
  return { scope, event_count: count, error_count: 0, quota_count: 0, unique_orgs: count ? 2 : 0,
    unique_agents: count ? 4 : 0, unattributed_count: 0, avg_latency_ms: count ? 25 : 0,
    by_kind: count ? [{ kind: 'search', event_count: count }] : [] }
}
export function dashboard(params, count = 100) {
  const tenant = params.tenant_id ?? null
  return { start: params.start, end: params.end, tenant_id: tenant, system_summary: summary(count),
    selected_summary: summary(tenant ? count / 4 : count, tenant ? 'org' : 'system'),
    timeseries: count ? [{ date: params.start.slice(0, 10), event_count: tenant ? count / 4 : count, error_count: 0, quota_count: 0 }] : [],
    orgs: count ? [{ tenant_id: otherCompanyId, name: 'Global leader', event_count: count / 2, quota_count: 0 }] : [],
    trending: count ? [{ query_hash: 'synthetic-query', query_normalized: tenant ? 'company query' : 'system query', kind: 'search', hits: 4, distinct_agents: 2, distinct_orgs: tenant ? 1 : 2 }] : [] }
}

export async function createHarness() {
  assert.equal(Boolean(process.env.OMO_BROWSER_ENGINE), false, 'Analytics QA requires a task-owned synthetic browser')
  const output = process.env.SEARCH_ANALYTICS_ARTIFACTS ?? await mkdtemp(join(tmpdir(), 'search-analytics-evidence-'))
  await mkdir(output, { recursive: true })
  const work = await mkdtemp(join(tmpdir(), 'search-analytics-runtime-'))
  const profile = join(work, 'chromium')
  const receipt = { engine: 'omowright.connectPipe with Playwright CDP assertions', output, work, scenarios: [], captures: [], requests: [], timings: [], blocked: [], cleanup: {} }
  let server, owned, browser, omowright, base
  try {
    await build({ configLoader: 'runner', cacheDir: join(work, 'vite-cache'), build: { outDir: join(work, 'dist'), emptyOutDir: true } })
    server = await preview({ configLoader: 'runner', build: { outDir: join(work, 'dist') }, preview: { host: '127.0.0.1', port: 0 }, plugins: [{ name: 'analytics-no-live-api', configurePreviewServer(instance) {
      instance.middlewares.use((request, response, next) => {
        if (!/^\/(api|ws|p)(\/|$)/.test(request.url ?? '')) return next()
        receipt.blocked.push(request.url)
        response.writeHead(599); response.end('Synthetic QA blocks live API traffic')
      })
    } }] })
    base = server.resolvedUrls.local[0]
    const skillRoot = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
    const { loadOmowright } = await import(pathToFileURL(join(skillRoot, 'scripts/omowright.mjs')).href)
    ;({ omowright } = await loadOmowright())
    owned = await omowright.connectPipe({ browserPath: process.env.CHROMIUM_EXECUTABLE ?? chromium.executablePath(), browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${profile}`], storageRoot: profile })
    const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`)
  } catch (error) { await close(false); throw error }

  async function capture(page, name) {
    const layout = await page.evaluate(() => ({
      viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio },
      documentWidth: document.documentElement.scrollWidth,
      scrollOwners: [...document.querySelectorAll('*')]
        .filter((element) => /auto|scroll/.test(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight)
        .map((element) => ({ tag: element.tagName, id: element.id, className: element.className, top: element.scrollTop, height: element.clientHeight, contentHeight: element.scrollHeight })),
    }))
    assert.equal(layout.documentWidth <= layout.viewport.width, true)
    const cdp = await page.context().newCDPSession(page)
    const { targetInfo } = await cdp.send('Target.getTargetInfo')
    const real = await owned.attachPage(targetInfo.targetId)
    const snapshot = omowright.compactSnapshot(await real.snapshot())
    const path = join(output, `${name}.png`)
    await writeFile(path, await real.screenshot())
    receipt.captures.push({ path, viewport: page.viewportSize(), layout, snapshot })
    await cdp.detach()
  }
  async function scenario(options, exercise) {
    const context = await browser.newContext({ ...(options.mobile ? devices['iPhone 14'] : { viewport: { width: options.width ?? 1440, height: 1000 } }), colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce', locale: 'en-US', timezoneId: 'UTC' })
    const state = { token: 'synthetic-analytics-token', user: { ...admin }, count: 100, status: 200, gate: null, exchanges: [], overrides: {} }
    const errors = []
    try {
      await context.addInitScript(() => { localStorage.setItem('maraclaw-admin-token', 'synthetic-analytics-token'); Date.now = () => new Date('2026-11-10T12:00:00Z').getTime() })
      await context.route('**/*', (route) => new URL(route.request().url()).origin === new URL(base).origin ? route.continue() : route.abort())
      const page = await context.newPage()
      page.setDefaultTimeout(15_000)
      page.on('pageerror', (error) => errors.push(error.message))
      await page.route('**/api/**', async (route) => {
        const url = new URL(route.request().url()), path = url.pathname, params = Object.fromEntries(url.searchParams)
        const exchange = { path, params, gate: state.gate, delivered: Promise.withResolvers(), at: performance.now(), status: state.status }
        state.exchanges.push(exchange)
        let body, status = 200
        if (path === '/api/auth/me') body = state.user
        else if (path === '/api/auth/login') body = { access_token: state.token, token_type: 'bearer', user: state.user }
        else if (path === '/api/notifications/unread-count') body = { unread_count: 0 }
        else if (path === '/api/admin/companies/options') body = [{ id: companyId, name: 'Selected company' }, { id: otherCompanyId, name: 'Global leader' }]
        else if (path === '/api/admin/tavily-keys') body = []
        else if (path === '/api/admin/search-analytics/export-status') body = { capture_enabled: true, export_enabled: false, include_raw: false, bucket: null, prefix: 'search/', pending: 3, exporting: 0, exported: 0, skipped: 0, last_exported_at: null }
        else if (path.startsWith('/api/admin/search-analytics/')) {
          const envelope = dashboard(params, state.count)
          const metric = path.split('/').at(-1)
          body = metric === 'summary' ? envelope.selected_summary : metric === 'dashboard' ? { ...envelope, ...state.overrides } : envelope[metric]
          status = state.status
          if (status !== 200) body = { detail: 'Synthetic analytics unavailable' }
          if (exchange.gate) await exchange.gate.promise
        } else { exchange.delivered.resolve(); throw new Error(`Unexpected synthetic API ${path}`) }
        try { await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }) }
        finally { exchange.delivered.resolve(); receipt.requests.push({ scenario: options.name, path, params, status }) }
      })
      const started = performance.now()
      await page.goto(`${base}${options.path ?? 'search-engine?tab=analytics'}`)
      await exercise(page, state)
      receipt.timings.push({ scenario: options.name, navigationThroughAssertionsMs: performance.now() - started })
      assert.deepEqual(errors, []); assert.deepEqual(receipt.blocked, [])
      receipt.scenarios.push(options.name); console.log(`PASS ${options.name}`)
    } finally {
      for (const exchange of state.exchanges) exchange.gate?.resolve()
      await Promise.all(state.exchanges.map((exchange) => exchange.delivered.promise))
      await context.close()
    }
  }
  async function close(completed) {
    receipt.completed = completed
    try { await browser?.close() }
    finally {
      try { await owned?.close(); receipt.cleanup.browserClosed = true }
      finally {
        try { if (server) await new Promise((resolve, reject) => server.httpServer.close((error) => error ? reject(error) : resolve())); receipt.cleanup.viteClosed = true }
        finally { await rm(work, { recursive: true, force: true }); receipt.cleanup.runtimeRemoved = true; await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2)); console.log(JSON.stringify({ completed, output, cleanup: receipt.cleanup })) }
      }
    }
  }
  return { base, scenario, capture, close, receipt }
}

export async function flush(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
}
