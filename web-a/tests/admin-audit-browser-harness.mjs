import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { chromium, devices, expect as playwrightExpect } from '@playwright/test'
import { createServer } from 'vite'

const expect = playwrightExpect.configure({ timeout: 15_000 })
export const tenantId = 'a8122022-c233-4f10-91df-91e2d065d2cb'
export const actorId = '66dccce3-bcfb-40ef-b334-49c2b47d614d'
export const event = {
  id: 'b0e03217-e53b-4e50-b4a8-1488d7d92b31',
  actor_id: actorId, actor_role: 'org_admin', actor_email: 'operator@example.test',
  action: 'user.activate', target_type: 'user', target_id: actorId, tenant_id: tenantId,
  changes: { is_active: { before: false, after: true } }, details: { note: '<script>literal, never executed</script>', nested: [1, true, null] },
  ip_address: '127.0.0.1', created_at: '2026-10-21T12:30:00Z',
}
export const nullableEvent = { ...event, id: '6f4ed823-44ad-4a0f-ab1b-f773be2a09fd', action: 'company.create', actor_id: null, actor_email: null, target_id: null, tenant_id: null, ip_address: null, created_at: '2026-10-20T12:30:00Z', changes: {}, details: {} }
const admin = {
  id: actorId, identity_id: actorId, email: 'operator@example.test', display_name: 'Operator',
  avatar_url: null, role: 'org_admin', is_platform_admin: false, tenant_id: tenantId, title: null, primary_mobile: null,
  registration_source: null, is_active: true, email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}

export async function createHarness() {
  assert.equal(Boolean(process.env.OMO_BROWSER_ENGINE), false, 'Audit QA requires an owned synthetic browser, not the user browser')
  const output = process.env.ADMIN_AUDIT_ARTIFACTS ?? await mkdtemp(join(tmpdir(), 'admin-audit-evidence-'))
  await mkdir(output, { recursive: true })
  const work = await mkdtemp(join(tmpdir(), 'admin-audit-runtime-'))
  const profile = join(work, 'chromium')
  const receipt = { startedAt: new Date().toISOString(), completed: false, engine: 'omowright.connectPipe; Playwright CDP assertion adapter', work, profile, obligations: ['Close owned browser and contexts', 'Close port-0 Vite server', 'Remove owned profile, cache and runtime root'], scenarios: [], screenshots: [], requests: [], snapshots: [], blockedServerRequests: [], cleanup: { browsersCreated: 0, browsersClosed: 0, contextsCreated: 0, contextsClosed: 0, browserClosed: false, viteClosed: false, runtimeRemoved: false, syntheticAPIsOnly: true } }
  await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  let server, browser, owned, omowright, base
  try {
    server = await createServer({ configLoader: 'runner', cacheDir: join(work, 'vite-cache'), server: { host: '127.0.0.1', port: 0, hmr: false, watch: { ignored: [output, work] } }, plugins: [{ name: 'audit-no-live-api', configureServer(instance) {
      instance.middlewares.use((request, response, next) => {
        if (!/^\/(api|ws|p)(\/|$)/.test(request.url ?? '')) return next()
        receipt.blockedServerRequests.push(request.url)
        response.writeHead(599, { 'Content-Type': 'application/json' })
        response.end(JSON.stringify({ detail: 'Audit harness refuses live API traffic' }))
      })
    } }] })
    await server.listen()
    base = server.resolvedUrls.local[0]
    receipt.vitePort = server.httpServer.address().port
    const skillRoot = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
    const { loadOmowright } = await import(pathToFileURL(join(skillRoot, 'scripts/omowright.mjs')).href)
    ;({ omowright } = await loadOmowright())
    owned = await omowright.connectPipe({ browserPath: process.env.CHROMIUM_EXECUTABLE ?? chromium.executablePath(), browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', '--remote-debugging-port=0', `--user-data-dir=${profile}`], storageRoot: profile })
    receipt.cleanup.browsersCreated++
    const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
    receipt.browserPort = Number(port)
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`)
    const cdp = await browser.newBrowserCDPSession()
    receipt.browserProcesses = (await cdp.send('SystemInfo.getProcessInfo')).processInfo
    await cdp.detach()
  } catch (error) {
    await close(false)
    throw error
  }

  async function scenario(options, exercise) {
    const errors = []
    const failedRequests = []
    const state = { user: { ...admin, ...options.user }, token: 'synthetic-audit-token', status: 200, result: [event, nullableEvent], nextCursor: options.nextCursor ?? null, gate: null, exchanges: [] }
    let context
    let page
    try {
      context = await browser.newContext({ ...(options.mobile ? devices['iPhone 14'] : { viewport: { width: options.width ?? 1440, height: 1000 } }), colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce', locale: 'en-US', timezoneId: 'UTC' })
      receipt.cleanup.contextsCreated++
      await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-audit-token'))
      await context.route('**/*', (route) => new URL(route.request().url()).origin === new URL(base).origin ? route.continue() : route.abort())
      page = await context.newPage()
      page.on('pageerror', (error) => errors.push(error.message))
      page.on('requestfailed', (request) => failedRequests.push({ path: new URL(request.url()).pathname, error: request.failure()?.errorText }))
      await page.route('**/api/**', async (route) => {
        const url = new URL(route.request().url())
        const path = url.pathname
        let body
        let status = 200
        let exchange
        if (path === '/api/auth/me') body = state.user
        else if (path === '/api/auth/login') body = { access_token: state.token, token_type: 'bearer', user: state.user }
        else if (path === '/api/notifications/unread-count') body = { unread_count: 0 }
        else if (path === '/api/admin/search-analytics/summary') body = { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' }
        else if (path === '/api/admin/search-analytics/orgs') body = []
        else if (path === '/api/admin/audit-logs/page' || path === '/api/users/admin-audit-logs/page') {
          exchange = { path, params: Object.fromEntries(url.searchParams), status: state.status, gate: state.gate, delivered: Promise.withResolvers() }
          state.exchanges.push(exchange)
          status = state.status
          body = status === 200 ? { items: state.result, next_cursor: state.nextCursor } : { detail: status === 400 ? 'Invalid cursor' : status === 403 ? 'Synthetic access denied' : 'Synthetic activity service unavailable' }
          if (exchange.gate) await exchange.gate.promise
          receipt.requests.push({ scenario: options.name, path, params: exchange.params, status })
        } else throw new Error(`Unexpected synthetic API ${route.request().method()} ${path}`)
        try { await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }) }
        finally { exchange?.delivered.resolve() }
      })
      await page.goto(`${base}activity`)
      await exercise(page, state)
      const cdp = await context.newCDPSession(page)
      const { targetInfo } = await cdp.send('Target.getTargetInfo')
      const realPage = await owned.attachPage(targetInfo.targetId)
      receipt.snapshots.push({ scenario: options.name, tree: omowright.compactSnapshot(await realPage.snapshot()) })
      await cdp.detach()
      assert.deepEqual(errors, [])
      assert.deepEqual(receipt.blockedServerRequests, [])
      receipt.scenarios.push(options.name)
      console.log(`PASS ${options.name}`)
    } catch (error) {
      receipt.failure = { scenario: options.name, errors, failedRequests, message: String(error), browserConnected: browser.isConnected(), pageClosed: page?.isClosed() }
      if (page && !page.isClosed()) {
        try {
          receipt.failure.body = await page.locator('body').innerText()
          await page.screenshot({ path: join(output, 'failed-scenario.png') })
        } catch (captureError) { receipt.failure.captureError = String(captureError) }
      }
      throw error
    } finally {
      for (const exchange of state.exchanges) exchange.gate?.resolve()
      await Promise.all(state.exchanges.map((exchange) => exchange.delivered.promise))
      if (context) {
        await context.close()
        receipt.cleanup.contextsClosed++
      }
    }
  }

  async function capture(page, name) {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    assert.equal(await page.locator('main').evaluate((element) => element.scrollWidth <= element.clientWidth), true)
    const path = join(output, `${name}.png`)
    await page.screenshot({ path, animations: 'disabled' })
    receipt.screenshots.push({ path, viewport: page.viewportSize() })
  }

  async function close(completed) {
    receipt.completed = completed
    receipt.finishedAt = new Date().toISOString()
    try {
      await browser?.close()
    } finally {
      try {
        await owned?.close()
        receipt.cleanup.browsersClosed = receipt.cleanup.browsersCreated
        receipt.cleanup.browserClosed = true
      } finally {
        try {
          await server?.close()
          receipt.cleanup.viteClosed = true
        } finally {
          await rm(work, { recursive: true, force: true })
          receipt.cleanup.runtimeRemoved = true
          await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
          console.log(JSON.stringify({ completed, cleanup: receipt.cleanup, scenarios: receipt.scenarios.length, screenshots: receipt.screenshots.length, requests: receipt.requests.length, output }))
        }
      }
    }
  }
  return { base, scenario, capture, close }
}

export async function flush(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
}

export async function signInReplacement(page) {
  await page.getByRole('button', { name: 'Sign out', exact: true }).click()
  await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
  await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await page.getByRole('heading', { name: 'Recent activity', exact: true }).waitFor()
  await expect(page.getByLabel('Action', { exact: true })).toHaveValue('')
}
