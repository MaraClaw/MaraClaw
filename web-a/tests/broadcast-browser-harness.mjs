import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { chromium, devices, expect } from '@playwright/test'
import { createServer } from 'vite'

export const broadcastId = '0d75651c-8c6c-4404-9046-50e1b3ff6dd2'
export const counts = { ok: true, broadcast_id: broadcastId, users_notified: 7, agents_notified: 3, emails_sent: 5 }
export const details = { 400: 'System email is not configured. Please configure it in Platform Settings.', 403: 'Only org admins can send broadcasts', 503: 'Synthetic service unavailable' }
export const admin = {
  id: 'operator', identity_id: 'identity', email: 'operator@example.test', display_name: 'Operator',
  avatar_url: null, role: 'org_admin', is_platform_admin: false, tenant_id: 'tenant-a', title: null, primary_mobile: null,
  registration_source: null, is_active: true, email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}
export const emptyStatus = { broadcast_id: broadcastId, available: false, queued: 0, claimed: 0, smtp_accepted: 0, failed: 0, cancelled: 0 }

export async function createBroadcastHarness() {
  if (process.env.OMO_BROWSER_ENGINE) throw new Error('Broadcast synthetic QA requires the explicitly permitted owned browser engine')
  const output = process.env.BROADCAST_ARTIFACTS ?? join(process.env.TMPDIR, 'broadcast-evidence')
  await mkdir(output, { recursive: true })
  const skillRoot = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
  const { loadOmowright } = await import(join(skillRoot, 'scripts/omowright.mjs'))
  const { omowright } = await loadOmowright()
  const server = await createServer({
    envDir: false, cacheDir: join(output, 'vite-cache'), define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    plugins: [{ name: 'broadcast-isolated-api', configResolved(config) { config.server.proxy = {} } }],
    server: { host: '127.0.0.1', port: 0, watch: { ignored: (path) => path.startsWith(output) } },
  })
  await server.listen()
  const base = server.resolvedUrls.local[0]
  const evidence = []
  const captures = []
  const ownedPages = new WeakMap()
  let closedContexts = 0
  let closedBrowsers = 0
  let closedSkillConnections = 0

  async function scenario(options, exercise) {
    const browser = await chromium.launch({ headless: true, args: ['--enable-automation', '--remote-debugging-port=0'] })
    let context
    let skillBrowser
    const state = {
      user: { ...admin, ...options.user }, token: 'synthetic-token', status: 200, gate: null, exchanges: [],
      receipt: counts, email: { ...emptyStatus, available: true, queued: 5 }, emailHttpStatus: 200, emailGate: null, emailRequests: [],
      cancelResult: { ...emptyStatus, available: true, cancelled: 5, cancelled_now: 5 }, cancelHttpStatus: 200, cancelGate: null, cancelRequests: [],
    }
    const errors = []
    try {
      context = await browser.newContext({ ...(options.mobile ? devices['iPhone 14'] : { viewport: { width: options.width ?? 1440, height: 1080 } }), colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce' })
      await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-token'))
      const page = await context.newPage()
      page.on('pageerror', (error) => errors.push(error.message))
      await page.route('**/api/**', async (route) => {
        const path = new URL(route.request().url()).pathname
        let body
        let status = 200
        let exchange
        if (path === '/api/auth/me') body = state.user
        else if (path === '/api/auth/login') body = { access_token: state.token, token_type: 'bearer', user: state.user }
        else if (path === '/api/notifications/unread-count') body = { unread_count: 0 }
        else if (path === '/api/admin/search-analytics/summary') body = { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' }
        else if (path === '/api/admin/search-analytics/orgs') body = []
        else if (path === '/api/notifications/broadcast' && route.request().method() === 'POST') {
          const payload = route.request().postDataJSON()
          exchange = { payload, status: state.status, gate: state.gate, delivered: Promise.withResolvers() }
          state.exchanges.push(exchange)
          body = payload.send_email ? state.receipt : { ...state.receipt, emails_sent: 0 }
        } else if (path === `/api/notifications/broadcast/${broadcastId}/email-status` && route.request().method() === 'GET') {
          exchange = { status: state.emailHttpStatus, gate: state.emailGate, delivered: Promise.withResolvers() }
          state.emailRequests.push(exchange)
          body = state.email
        } else if (path === `/api/notifications/broadcast/${broadcastId}/cancel-email` && route.request().method() === 'POST') {
          assert.equal(route.request().postData(), null)
          exchange = { status: state.cancelHttpStatus, gate: state.cancelGate, delivered: Promise.withResolvers() }
          state.cancelRequests.push(exchange)
          body = state.cancelResult
        } else {
          errors.push(`Unexpected synthetic API ${route.request().method()} ${path}`)
          await route.abort()
          return
        }
        try {
          if (exchange?.gate) await exchange.gate.promise
          status = exchange?.status ?? status
          await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status === 200 ? body : { detail: details[status] }) })
        } finally { exchange?.delivered.resolve() }
      })
      await page.goto(`${base}broadcast`)
      const cdp = await browser.newBrowserCDPSession()
      try {
        const { arguments: args } = await cdp.send('Browser.getBrowserCommandLine')
        const profile = args.find((arg) => arg.startsWith('--user-data-dir=')).slice('--user-data-dir='.length)
        const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
        skillBrowser = await omowright.connect(`http://127.0.0.1:${port}`)
        const target = (await skillBrowser.listTargets()).find((tab) => tab.url?.startsWith(base))
        assert.ok(target, 'Owned QA page must be present in the browser skill target list')
        ownedPages.set(page, await skillBrowser.attachPage(target.id))
      } finally { await cdp.detach() }
      await exercise(page, state)
      assert.deepEqual(errors, [])
      evidence.push(options.name)
      console.log(`PASS ${options.name}`)
    } finally {
      const requests = [...state.exchanges, ...state.emailRequests, ...state.cancelRequests]
      for (const exchange of requests) exchange.gate?.resolve()
      await Promise.allSettled(requests.map((exchange) => exchange.delivered.promise))
      if (skillBrowser) { await skillBrowser.close(); closedSkillConnections++ }
      await context?.close(); closedContexts++
      await browser.close(); closedBrowsers++
    }
  }

  async function capture(page, name) {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    const ownedPage = ownedPages.get(page)
    assert.ok(ownedPage)
    await writeFile(join(output, `${name}.png`), await ownedPage.screenshot())
    await writeFile(join(output, `${name}.json`), JSON.stringify(omowright.compactSnapshot(await ownedPage.snapshot()), null, 2))
    captures.push(name)
  }

  async function close(completed) {
    await server.close()
    const receipt = { completed, passed: evidence.length, scenarios: evidence, captures, output, cleanup: { closedContexts, closedBrowsers, closedSkillConnections, viteClosed: true, syntheticAPIsOnly: true } }
    await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
    console.log(JSON.stringify(receipt))
  }
  return { base, output, scenario, capture, close }
}

export async function compose(page, draft = { title: '  Maintenance  ', body: 'First line\nSecond line', send_email: true }) {
  await page.getByLabel('Title', { exact: true }).fill(draft.title)
  await page.getByLabel('Body (optional)', { exact: true }).fill(draft.body)
  await page.getByLabel('Also send email', { exact: true }).setChecked(draft.send_email)
  await page.getByRole('button', { name: 'Review broadcast', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Review broadcast' })).toBeFocused()
}

export async function flush(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
}

export async function sendReceipt(page, draft) {
  await compose(page, draft)
  await page.getByRole('button', { name: 'Send broadcast', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Broadcast sent' })).toBeVisible()
  return page.getByRole('region', { name: 'Email progress', exact: true })
}

export async function metric(progress, label, value) {
  await expect(progress.getByText(label, { exact: true }).locator('..').locator('dd')).toHaveText(String(value))
}

export async function confirmCancellation(progress) {
  await progress.getByRole('button', { name: 'Cancel queued email', exact: true }).click()
  await expect(progress.getByRole('heading', { name: 'Cancel queued email?', exact: true })).toBeFocused()
  await progress.getByRole('button', { name: 'Confirm cancel queued email', exact: true }).click()
}
