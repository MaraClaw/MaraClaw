import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { chromium, devices, expect as playwrightExpect } from '@playwright/test'
import { createServer } from 'vite'
import { adminUser } from './helpers.ts'
import { agentId, csvBody, episodeCursor, episodeRow, quietSummaryRow, summaryRow, tenantId } from './resource-pressure-fixtures.ts'

export const expect = playwrightExpect.configure({ timeout: 10_000 })
export const originalSearch = '?since=2026-11-01&until=2026-11-15&tier=pro&resource=memory'
// Initech is never on the browse page: only its by-id lookup or a picker search names it. Any other id answers 404.
export const archivedTenantId = '7c2e9b14-0d3a-4f6b-8e15-3a9d2c4b6e70'
const archived = { tenant_id: archivedTenantId, tenant_name: 'Initech' }
const companyOption = (row) => ({ id: row.tenant_id, name: row.tenant_name, slug: row.tenant_name.toLowerCase(), is_active: true, can_disable: true, tier: 'pro' })

export async function createHarness({ match } = {}) {
  assert.equal(Boolean(process.env.OMO_BROWSER_ENGINE), false, 'Upgrade signals QA needs an owned synthetic browser')
  const output = process.env.UPGRADE_SIGNALS_ARTIFACTS ?? await mkdtemp(join(tmpdir(), 'upgrade-signals-evidence-'))
  await mkdir(output, { recursive: true })
  const work = await mkdtemp(join(tmpdir(), 'upgrade-signals-runtime-'))
  const profile = join(work, 'chromium')
  const selected = match ? new RegExp(match) : null
  const receipt = { output, work, match, scenarios: [], captures: [], requests: [], blocked: [], cleanup: { contextsOpened: 0, contextsClosed: 0, browserClosed: false, viteClosed: false, runtimeRemoved: false } }
  let server, owned, browser, omowright, base
  async function close(completed) {
    receipt.completed = completed && receipt.scenarios.length > 0
    try { await browser?.close() }
    finally {
      try { await owned?.close(); receipt.cleanup.browserClosed = true }
      finally {
        try { await server?.close(); receipt.cleanup.viteClosed = true }
        finally {
          await rm(work, { recursive: true, force: true })
          receipt.cleanup.runtimeRemoved = true
          await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
          console.log(JSON.stringify(receipt.cleanup), output)
        }
      }
    }
    if (completed) assert.ok(receipt.scenarios.length > 0, 'At least one browser scenario must run')
  }
  try {
    server = await createServer({ configLoader: 'runner', cacheDir: join(work, 'vite'), server: { host: '127.0.0.1', port: 0, hmr: false }, plugins: [{ name: 'upgrade-signals-no-live-api', configureServer(instance) {
      instance.middlewares.use((request, response, next) => {
        if (!/^\/(api|ws|p)(\/|$)/.test(request.url ?? '')) return next()
        receipt.blocked.push(request.url)
        response.writeHead(599); response.end('Synthetic traffic only')
      })
    } }] })
    await server.listen()
    base = server.resolvedUrls.local[0]
    const skillRoot = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
    const { loadOmowright } = await import(pathToFileURL(join(skillRoot, 'scripts/omowright.mjs')).href)
    ;({ omowright } = await loadOmowright())
    owned = await omowright.connectPipe({ browserPath: process.env.CHROMIUM_EXECUTABLE ?? chromium.executablePath(), browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${profile}`], storageRoot: profile })
    owned.browserProcess.once('exit', (code, signal) => { receipt.browserExit = { code, signal, at: new Date().toISOString() } })
    owned.browserProcess.stderr.on('data', (chunk) => { receipt.browserStderr = `${receipt.browserStderr ?? ''}${chunk}`.slice(-4000) })
    const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`)
    browser.on('disconnected', () => { receipt.disconnectedAt = new Date().toISOString() })
    receipt.ports = { vite: server.httpServer.address().port, browser: Number(port) }
  } catch (error) { await close(false); throw error }

  async function scenario(options, exercise) {
    if (selected && !selected.test(options.name)) return
    const context = await browser.newContext({ ...(options.mobile ? devices['iPhone 14'] : { viewport: { width: 1440, height: 1000 } }), colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce', locale: 'en-US', timezoneId: 'UTC' })
    receipt.cleanup.contextsOpened++
    const state = { summaryStatus: options.summaryStatus ?? 200, episodeStatus: options.episodeStatus ?? 200, rows: options.rows ?? [summaryRow, quietSummaryRow], requests: [], errors: [] }
    const page = await context.newPage()
    try {
      await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-pressure-token'))
      await context.route('**/*', (route) => new URL(route.request().url()).origin === new URL(base).origin ? route.continue() : route.abort())
      page.on('pageerror', (error) => state.errors.push(error.message))
      page.on('crash', () => state.errors.push('Browser renderer crashed'))
      await page.route('**/api/**', async (route) => {
        const request = route.request(), url = new URL(request.url()), path = url.pathname
        const params = Object.fromEntries(url.searchParams)
        const exchange = { path, params, method: request.method() }
        state.requests.push(exchange); receipt.requests.push({ scenario: options.name, ...exchange })
        assert.equal(request.method(), 'GET', 'QA must not mutate a live or synthetic API')
        let body, status = 200
        if (path === '/api/auth/me') body = { ...adminUser(agentId), identity_id: agentId, tenant_id: tenantId, role: options.orgAdmin ? 'org_admin' : 'platform_admin', is_platform_admin: !options.orgAdmin }
        else if (path === '/api/notifications/unread-count') body = { unread_count: 0 }
        else if (path === '/api/admin/companies/options') {
          const matches = params.q ? [...state.rows, archived].filter((row) => row.tenant_name.toLowerCase().includes(params.q.toLowerCase())) : state.rows
          body = { items: matches.map(companyOption), next_cursor: null }
        } else if (path.startsWith('/api/tenants/')) {
          const row = [...state.rows, archived].find((item) => path === `/api/tenants/${item.tenant_id}`)
          if (row) body = companyOption(row)
          else { status = 404; body = { detail: 'Tenant not found' } }
        }
        else if (path.endsWith('/resource-pressure/summary')) {
          status = state.summaryStatus
          body = status === 200 ? state.rows.filter((row) => !params.tenant_id || row.tenant_id === params.tenant_id) : { detail: 'Synthetic summary unavailable' }
        } else if (path.endsWith('/resource-pressure/episodes')) {
          status = state.episodeStatus
          const filtered = params.agent_id || params.kind || params.open_only
          const item = { ...episodeRow, agent_name: params.cursor ? 'Older assistant' : filtered ? 'Filtered assistant' : 'Sales assistant' }
          body = status === 200 ? { items: options.emptyEpisodes ? [] : [item], next_cursor: params.cursor ? null : episodeCursor } : { detail: 'Synthetic episodes unavailable' }
        } else if (path.endsWith('/resource-pressure/export.csv')) {
          await route.fulfill({ status: 200, contentType: 'text/csv', body: csvBody }); return
        } else if (path === '/api/admin/search-analytics/summary') body = { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' }
        else if (path === '/api/admin/search-analytics/orgs') body = []
        else throw new Error(`Unexpected synthetic API ${path}`)
        await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
      })
      await page.goto(`${base}${options.path ?? `upgrade-signals${originalSearch}`}`)
      await exercise(page, state)
      assert.deepEqual(state.errors, [])
      assert.deepEqual(receipt.blocked, [])
      receipt.scenarios.push(options.name)
      console.log(`PASS ${options.name}`)
    } catch (error) {
      receipt.failure = { scenario: options.name, message: String(error), errors: state.errors, browserConnected: browser.isConnected(), pageClosed: page.isClosed() }
      if (browser.isConnected() && !page.isClosed()) {
        receipt.failure.body = await page.locator('body').innerText()
        await page.screenshot({ path: join(output, 'failure.png') })
      }
      throw error
    } finally { await context.close(); receipt.cleanup.contextsClosed++ }
  }

  async function capture(page, name) {
    const cdp = await page.context().newCDPSession(page)
    const { targetInfo } = await cdp.send('Target.getTargetInfo')
    const realPage = await owned.attachPage(targetInfo.targetId)
    const tree = omowright.compactSnapshot(await realPage.snapshot())
    const path = join(output, `${name}.png`)
    await writeFile(path, await realPage.screenshot())
    await cdp.detach()
    const probe = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, overflow: document.documentElement.scrollWidth > innerWidth, scrollers: [...document.querySelectorAll('*')].filter((el) => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight).map((el) => el.id || el.tagName) }))
    assert.equal(probe.overflow, false)
    receipt.captures.push({ path, probe, tree })
  }
  return { scenario, capture, close, base, output }
}
