import assert from 'node:assert/strict'
import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { chromium, devices } from '@playwright/test'
import { createServer } from 'vite'

export const tenantA = '11111111-1111-4111-8111-111111111111'
export const tenantB = '22222222-2222-4222-8222-222222222222'
export const initialEmail = { enabled: true, from_address: 'mail@example.test', from_name: 'MaraClaw', smtp_host: 'smtp.example.test', smtp_port: 465, smtp_username: 'mailer', smtp_ssl: true, smtp_timeout_seconds: 15, has_password: true, effective_source: 'tenant' }
export const emptyEmail = { enabled: false, from_address: '', from_name: '', smtp_host: '', smtp_port: 465, smtp_username: '', smtp_ssl: true, smtp_timeout_seconds: 15, has_password: false, effective_source: 'none' }
const admin = { id: 'operator', identity_id: 'identity', email: 'operator@example.test', display_name: 'Operator', avatar_url: null, role: 'platform_admin', is_platform_admin: true, tenant_id: tenantA, is_genesis: true, title: null, primary_mobile: null, registration_source: null, is_active: true, email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z' }
const companies = [tenantA, tenantB].map((id, index) => ({ id, name: index ? 'KABERs' : 'MaraClaw', slug: index ? 'kabers' : 'maraclaw', is_active: true, is_system: true, is_default_end_user_org: !index, can_disable: false, sso_enabled: false, sso_domain: null, created_at: null, user_count: 1, active_user_count: 1, inactive_user_count: 0, agent_count: 0, org_admin_email: null }))

export async function createEmailHarness() {
  if (process.env.OMO_BROWSER_ENGINE) throw new Error('Synthetic SMTP QA requires the permitted owned browser engine')
  const output = await mkdtemp(join(process.env.SMTP_ARTIFACTS ?? tmpdir(), 'tenant-email-'))
  const skillRoot = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
  const { loadOmowright } = await import(join(skillRoot, 'scripts/omowright.mjs'))
  const { omowright } = await loadOmowright()
  const server = await createServer({ envDir: false, cacheDir: join(output, 'vite-cache'), define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') }, plugins: [{ name: 'smtp-isolated-api', configResolved(config) { config.server.proxy = {} } }], server: { host: '127.0.0.1', port: 0, watch: { ignored: (path) => path.startsWith(output) } } })
  const receipt = { output, scenarios: [], captures: [], scrollOwners: [], cleanup: { closedContexts: 0, browserClosed: true, skillClosed: true, viteClosed: false, syntheticAPIsOnly: true } }
  await server.listen()
  const base = server.resolvedUrls.local[0]
  return {
    output,
    async scenario(options, exercise) {
      let browser, skillBrowser, context
      const state = { user: { ...admin, ...options.user }, configs: { [tenantA]: { ...initialEmail }, [tenantB]: { ...emptyEmail } }, requests: [], auxiliaryRequests: [], getStatus: options.getStatus ?? 200, getGate: options.getGate, putGate: null, putStatus: 200, testStatus: 200, deleteStatus: 204 }
      const errors = []
      try {
        browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE ?? chromium.executablePath(), headless: true, args: ['--enable-automation', '--remote-debugging-port=0'] })
        const cdp = await browser.newBrowserCDPSession()
        try {
          const { arguments: args } = await cdp.send('Browser.getBrowserCommandLine')
          const profile = args.find((arg) => arg.startsWith('--user-data-dir=')).slice('--user-data-dir='.length)
          const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
          skillBrowser = await omowright.connect(`http://127.0.0.1:${port}`)
        } finally { await cdp.detach() }
        context = await browser.newContext({ ...(options.width === 390 ? devices['iPhone 14'] : { viewport: { width: options.width ?? 1440, height: 1080 } }), colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce' })
        context.setDefaultTimeout(10_000)
        await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-smtp-token'))
        const page = await context.newPage()
        page.on('pageerror', (error) => errors.push(error.message))
        await page.route('**/api/**', async (route) => {
          const request = route.request(), url = new URL(request.url()), path = url.pathname
          if (path.startsWith('/api/enterprise/tenant-email')) {
            const method = request.method(), tenant = url.searchParams.get('tenant_id') ?? state.user.tenant_id
            const status = method === 'GET' ? state.getStatus : method === 'PUT' ? state.putStatus : method === 'DELETE' ? state.deleteStatus : state.testStatus
            const exchange = { method, tenant, body: request.postDataJSON(), status, gate: method === 'GET' ? state.getGate : state.putGate, delivered: Promise.withResolvers() }
             state.requests.push(exchange)
             try {
              assert.equal(tenant, state.user.tenant_id, 'SMTP request must use current own organization')
              assert.equal(state.user.is_active && state.user.is_genesis === true && ['org_admin', 'platform_admin'].includes(state.user.role), true, 'SMTP request requires active genesis admin')
              await exchange.gate?.promise
              if (status === 0) { await route.abort('connectionfailed'); return }
              if (status >= 400) { await route.fulfill({ status, json: { detail: `Synthetic SMTP error ${status}` } }); return }
              if (method === 'PUT') { const { password, ...fields } = exchange.body; state.configs[tenant] = { ...fields, has_password: Boolean(password) || state.configs[tenant].has_password, effective_source: fields.enabled ? 'tenant' : 'none' } }
              if (method === 'DELETE') state.configs[tenant] = { ...emptyEmail }
              await route.fulfill(method === 'DELETE' ? { status: 204 } : { json: method === 'POST' ? { message: 'Test email sent to operator@example.test.' } : state.configs[tenant] })
            } finally { exchange.delivered.resolve() }
            return
          }
          if (path === '/api/admin/companies') { state.auxiliaryRequests.push(path); errors.push('SMTP must never request a company list'); await route.abort(); return }
          if (path.startsWith('/api/tenants/')) state.auxiliaryRequests.push(path)
          const body = path === '/api/auth/me' ? state.user : path === '/api/tenants/me' ? companies.find((company) => company.id === state.user.tenant_id) : path.startsWith('/api/tenants/') ? companies.find((company) => path.endsWith(company.id)) : path === '/api/admin/transfer-policy' ? { fee_amount_minor: 3719, currency: 'EUR', instructions: 'Arrange payment with the platform team.' } : path === '/api/notifications/unread-count' ? { unread_count: 0 } : path === '/api/admin/search-analytics/summary' ? { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' } : path === '/api/admin/search-analytics/orgs' ? [] : null
          if (!body) { errors.push(`Unexpected synthetic API ${request.method()} ${path}`); await route.abort(); return }
          await route.fulfill({ json: body })
        })
        await page.goto(`${base}settings`)
        const target = (await skillBrowser.listTargets()).find((tab) => tab.url?.startsWith(base))
        assert.ok(target)
        const ownedPage = await skillBrowser.attachPage(target.id)
        await exercise(page, state, async (name) => {
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
          assert.equal(await page.locator('main').evaluate((element) => element.scrollWidth <= element.clientWidth), true)
          const path = join(output, `${options.name}-${name}.png`)
          await writeFile(path, await ownedPage.screenshot())
          receipt.captures.push(path)
          receipt.scrollOwners.push({ name: `${options.name}-${name}`, owners: await page.evaluate(() => [...document.querySelectorAll('*')].filter((el) => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight).map((el) => el.tagName.toLowerCase())) })
        })
        assert.deepEqual(errors, [])
        assert.equal(state.auxiliaryRequests.includes('/api/admin/companies'), false)
        receipt.scenarios.push(options.name)
        console.log(`PASS ${options.name}`)
      } finally {
        for (const request of state.requests) request.gate?.resolve()
        await Promise.allSettled(state.requests.map((request) => request.delivered.promise))
        await context?.close(); receipt.cleanup.closedContexts++
        await skillBrowser?.close()
        await browser?.close()
      }
    },
    async close() {
      await server.close(); receipt.cleanup.viteClosed = true
      await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
      console.log(JSON.stringify(receipt))
    },
  }
}
