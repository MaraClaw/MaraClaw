import assert from 'node:assert/strict'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
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
export const nullableEvent = { ...event, id: '6f4ed823-44ad-4a0f-ab1b-f773be2a09fd', action: 'company.create', actor_id: null, actor_email: null, target_id: null, tenant_id: null, ip_address: null, created_at: null, changes: {}, details: {} }
const admin = {
  id: actorId, identity_id: actorId, email: 'operator@example.test', display_name: 'Operator',
  avatar_url: null, role: 'org_admin', is_platform_admin: false, tenant_id: tenantId, title: null, primary_mobile: null,
  registration_source: null, is_active: true, email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}

export async function createHarness() {
  const output = process.env.ADMIN_AUDIT_ARTIFACTS ?? await mkdtemp(join(tmpdir(), 'admin-audit-evidence-'))
  await mkdir(output, { recursive: true })
  const server = await createServer({ cacheDir: join(output, 'vite-cache'), server: { host: '127.0.0.1', port: 0, watch: { ignored: (path) => path.startsWith(output) } } })
  const receipt = { startedAt: new Date().toISOString(), completed: false, scenarios: [], screenshots: [], requests: [], cleanup: { browsersCreated: 0, browsersClosed: 0, contextsCreated: 0, contextsClosed: 0, browserClosed: false, viteClosed: false, syntheticAPIsOnly: true } }
  try {
    await server.listen()
  } catch (error) {
    await server.close()
    throw error
  }
  const base = server.resolvedUrls.local[0]

  async function scenario(options, exercise) {
    const browser = await chromium.launch({ headless: true })
    receipt.cleanup.browsersCreated++
    const errors = []
    const failedRequests = []
    const state = { user: { ...admin, ...options.user }, token: 'synthetic-audit-token', status: 200, result: [event, nullableEvent], gate: null, exchanges: [] }
    let context
    let page
    try {
      context = await browser.newContext({ ...(options.mobile ? devices['iPhone 14'] : { viewport: { width: options.width ?? 1440, height: 1000 } }), colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce' })
      receipt.cleanup.contextsCreated++
      await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-audit-token'))
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
        else if (path === '/api/admin/audit-logs' || path === '/api/users/admin-audit-logs') {
          exchange = { path, params: Object.fromEntries(url.searchParams), status: state.status, gate: state.gate, delivered: Promise.withResolvers() }
          state.exchanges.push(exchange)
          status = state.status
          body = status === 200 ? state.result : { detail: status === 403 ? 'Synthetic access denied' : 'Synthetic activity service unavailable' }
          if (exchange.gate) await exchange.gate.promise
          receipt.requests.push({ scenario: options.name, path, params: exchange.params, status })
        } else throw new Error(`Unexpected synthetic API ${route.request().method()} ${path}`)
        try { await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) }) }
        finally { exchange?.delivered.resolve() }
      })
      await page.goto(`${base}activity`)
      await exercise(page, state)
      assert.deepEqual(errors, [])
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
      await browser.close()
      receipt.cleanup.browsersClosed++
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
    receipt.cleanup.browserClosed = receipt.cleanup.browsersCreated === receipt.cleanup.browsersClosed
    await server.close()
    receipt.cleanup.viteClosed = true
    await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
    console.log(JSON.stringify({ ...receipt, screenshots: receipt.screenshots.length, requests: receipt.requests.length, output }))
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
