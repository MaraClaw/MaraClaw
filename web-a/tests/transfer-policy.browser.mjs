import assert from 'node:assert/strict'
import { mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { chromium, devices, expect } from '@playwright/test'
import { createServer } from 'vite'

if (process.env.OMO_BROWSER_ENGINE) throw new Error('Synthetic Settings QA requires the permitted owned browser engine')
const output = await mkdtemp(join(tmpdir(), 'transfer-policy-settings-'))
const skillRoot = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
const { loadOmowright } = await import(join(skillRoot, 'scripts/omowright.mjs'))
const { omowright } = await loadOmowright()
const server = await createServer({
  envDir: false, cacheDir: join(output, 'vite-cache'), define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
  plugins: [{ name: 'settings-isolated-api', configResolved(config) { config.server.proxy = {} } }],
  server: { host: '127.0.0.1', port: 0, watch: { ignored: (path) => path.startsWith(output) } },
})
const scenarios = []
const captures = []
const admin = {
  id: 'operator', identity_id: 'identity', email: 'operator@example.test', display_name: 'Operator',
  avatar_url: null, role: 'platform_admin', is_platform_admin: true, tenant_id: null, title: null, primary_mobile: null,
  registration_source: null, is_active: true, email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}
const original = { fee_amount_minor: 3719, currency: 'EUR', instructions: 'Contact the platform team to arrange payment.\nInclude your request reference.' }
let browser
let skillBrowser
let base
let closedContexts = 0

async function scenario(options, exercise) {
  browser = await chromium.launch({ executablePath: chromium.executablePath(), headless: true, args: ['--enable-automation', '--remote-debugging-port=0'] })
  const cdp = await browser.newBrowserCDPSession()
  try {
    const { arguments: args } = await cdp.send('Browser.getBrowserCommandLine')
    const profile = args.find((arg) => arg.startsWith('--user-data-dir=')).slice('--user-data-dir='.length)
    const [port] = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')
    skillBrowser = await omowright.connect(`http://127.0.0.1:${port}`)
  } finally { await cdp.detach() }
  const context = await browser.newContext({
    ...(options.width === 390 ? devices['iPhone 14'] : { viewport: { width: options.width ?? 1440, height: 1080 } }),
    colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce',
  })
  context.setDefaultTimeout(10_000)
  const errors = []
  const state = { user: { ...admin, ...options.user }, policy: { ...original, ...options.policy }, getStatus: options.getStatus ?? 200, putStatus: 200, getGate: options.getGate, putGate: null, saved: null, requests: [] }
  try {
    await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-settings-token'))
    const page = await context.newPage()
    page.on('pageerror', (error) => errors.push(error.message))
    await page.route('**/api/**', async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (path === '/api/admin/transfer-policy') {
        const method = request.method()
        const exchange = { method, body: method === 'PUT' ? request.postDataJSON() : null, status: method === 'PUT' ? state.putStatus : state.getStatus, gate: method === 'PUT' ? state.putGate : state.getGate, delivered: Promise.withResolvers() }
        state.requests.push(exchange)
        try {
          if (exchange.gate) await exchange.gate.promise
          if (exchange.status === 0) { await route.abort('connectionfailed'); return }
          if (method === 'PUT' && exchange.status === 200) state.policy = state.saved ?? exchange.body
          await route.fulfill({ status: exchange.status, json: exchange.status === 200 ? state.policy : { detail: `Synthetic policy error ${exchange.status}` } })
        } finally { exchange.delivered.resolve() }
        return
      }
      const body = path === '/api/admin/companies' ? []
            : path.startsWith('/api/enterprise/tenant-email') ? { enabled: false, from_address: '', from_name: '', smtp_host: '', smtp_port: 465, smtp_username: '', smtp_ssl: true, smtp_timeout_seconds: 15, has_password: false, effective_source: 'none' }
        : path === '/api/auth/me' ? state.user
        : path === '/api/notifications/unread-count' ? { unread_count: 0 }
          : path === '/api/admin/search-analytics/summary' ? { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' }
            : path === '/api/admin/search-analytics/orgs' ? [] : null
      if (body === null) { errors.push(`Unexpected synthetic API ${request.method()} ${path}`); await route.abort(); return }
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
      captures.push(path)
    })
    assert.deepEqual(errors, [])
    scenarios.push(options.name)
    console.log(`PASS ${options.name}`)
  } catch (error) {
    const page = context.pages()[0]
    await writeFile(join(output, `${options.name}-failure.json`), JSON.stringify({ requests: state.requests.map(({ method, body, status }) => ({ method, body, status })), policy: state.policy, text: await page?.locator('main').innerText() }, null, 2))
    throw error
  } finally {
    for (const request of state.requests) request.gate?.resolve()
    await Promise.allSettled(state.requests.map((request) => request.delivered.promise))
    await context.close()
    await skillBrowser.close(); skillBrowser = undefined
    await browser.close(); browser = undefined
    closedContexts++
  }
}

try {
  await server.listen()
  base = server.resolvedUrls.local[0]

  for (const [role, platform, allowed] of [
    ['org_admin', false, false], ['platform_admin', false, true], ['org_admin', true, true],
  ]) await scenario({ name: `role-${role}-${platform}`, user: { role, is_platform_admin: platform } }, async (page, state, capture) => {
    // Given / When: navigation to the real Settings route with this synthetic identity.
    await expect(page.getByRole('heading', { name: 'Session', exact: true })).toBeVisible()
    // Then
    await expect(page.getByRole('region', { name: 'Transfer fee and instructions' })).toHaveCount(allowed ? 1 : 0)
    if (allowed) await expect(page.getByLabel('Fee amount', { exact: true })).toHaveValue('37.19')
    else { assert.equal(state.requests.length, 0); await capture('session-only') }
  })

  await scenario({ name: 'password-change', user: { must_change_password: true } }, async (page, state) => {
    // Given / When: Settings is still allowed during mandatory password change.
    await expect(page).toHaveURL(`${base}settings`)
    // Then
    await expect(page.getByRole('region', { name: 'Transfer fee and instructions' }).getByRole('link', { name: 'Account' })).toBeVisible()
    await expect(page.getByLabel('Fee amount', { exact: true })).toHaveCount(0)
    assert.equal(state.requests.length, 0)
  })

  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) {
    const getGate = Promise.withResolvers()
    await scenario({ name: `${width}-${scheme}`, width, scheme, getGate, getStatus: 503 }, async (page, state, capture) => {
      // Given: unavailable policy never supplies invented field defaults.
      await expect(page.getByText('Loading transfer policy…', { exact: true })).toBeVisible()
      await expect(page.getByLabel('Fee amount', { exact: true })).toHaveCount(0)
      await capture('loading')
      getGate.resolve()
      await expect(page.getByRole('alert')).toHaveText('Synthetic policy error 503')
      await capture('load-error')
      state.getGate = null; state.getStatus = 200
      await page.getByRole('button', { name: 'Retry loading policy' }).click()
      const amount = page.getByLabel('Fee amount', { exact: true })
      const currency = page.getByLabel('Currency', { exact: true })
      const instructions = page.getByLabel('Payment instructions', { exact: true })
      const saved = page.getByRole('region', { name: 'Current saved policy' })
      await expect(amount).toHaveValue('37.19')
      await expect(currency).toHaveValue('EUR')
      await expect(instructions).toHaveValue(original.instructions)
      await page.locator('main').evaluate((element) => { element.scrollTop = 0 })
      await capture('saved-top')
      await page.getByRole('button', { name: 'Save transfer policy', exact: true }).scrollIntoViewIfNeeded()
      await capture('saved-end')
      await amount.fill('1.005')
      await page.getByRole('button', { name: 'Save transfer policy', exact: true }).click()
      await expect(amount).toBeFocused()
      await expect(amount).toHaveAttribute('aria-invalid', 'true')
      await capture('validation')
      await amount.fill('0.29'); await currency.fill('gbp'); await instructions.fill('Updated manual payment instructions.\nReference your transfer request.')
      state.putStatus = 0; state.putGate = Promise.withResolvers()
      // When: a double submit races the pending render and its response is lost.
      await page.getByRole('button', { name: 'Save transfer policy', exact: true }).evaluate((button) => { button.click(); button.click() })
      await expect(page.getByRole('button', { name: 'Saving policy…' })).toBeDisabled()
      await expect(amount).toBeDisabled(); await expect(currency).toBeDisabled(); await expect(instructions).toBeDisabled()
      assert.equal(state.requests.filter((request) => request.method === 'PUT').length, 1)
      await capture('pending')
      state.putGate.resolve()
      // Then: retain the draft and persisted state; successful retry uses server truth.
      await expect(page.getByRole('alert')).toContainText('Could not confirm')
      await expect(amount).toHaveValue('0.29')
      await expect(saved).toContainText('EUR 37.19')
      await capture('save-error')
      state.putStatus = 200; state.putGate = null
      state.saved = { fee_amount_minor: 31, currency: 'GBP', instructions: 'Persisted server instructions.\nVerify manually.' }
      await page.getByRole('button', { name: 'Save transfer policy', exact: true }).click()
      await expect(amount).toHaveValue('0.31')
      await expect(currency).toHaveValue('GBP')
      await expect(instructions).toHaveValue(state.saved.instructions)
      await expect(saved).toContainText('GBP 0.31')
      assert.deepEqual(state.requests.filter((request) => request.method === 'PUT')[1].body, { fee_amount_minor: 29, currency: 'GBP', instructions: 'Updated manual payment instructions.\nReference your transfer request.' })
      await expect(page.getByRole('status').filter({ hasText: 'Transfer policy saved.' })).toBeVisible()
      await capture('success-end')
      await page.reload()
      await expect(amount).toHaveValue('0.31')
    })
  }

  for (const width of [390, 1440]) for (const scheme of ['light', 'dark']) {
    for (const [currency, minor, decimal, invalid] of [
      ['USD', Number.MAX_SAFE_INTEGER, '90071992547409.91', '90071992547409.92'],
      ['EUR', 2500, '25.00', '1.001'], ['GBP', 29, '0.29', '1.001'],
      ['VND', 123, '123', '1.0'], ['JPY', Number.MAX_SAFE_INTEGER, '9007199254740991', '0.01'],
      ['KWD', Number.MAX_SAFE_INTEGER, '9007199254740.991', '9007199254740.992'],
    ]) await scenario({ name: `currency-${currency}-${width}-${scheme}`, width, scheme, policy: { currency, fee_amount_minor: minor } }, async (page, state, capture) => {
      const amount = page.getByLabel('Fee amount', { exact: true })
      const saved = page.getByRole('region', { name: 'Current saved policy' })
      await expect(amount).toHaveValue(decimal)
      await expect(saved).toContainText(`${currency} ${decimal}`)
      await amount.fill(invalid)
      await page.getByRole('button', { name: 'Save transfer policy', exact: true }).click()
      await expect(amount).toHaveAttribute('aria-invalid', 'true')
      assert.equal(state.requests.filter((request) => request.method === 'PUT').length, 0)
      await capture('invalid-amount')
      await amount.fill(decimal)
      await page.getByRole('button', { name: 'Save transfer policy', exact: true }).click()
      await expect(page.getByRole('status').filter({ hasText: 'Transfer policy saved.' })).toBeVisible()
      assert.deepEqual(state.requests.find((request) => request.method === 'PUT').body, { ...original, currency, fee_amount_minor: minor })
      await page.reload()
      await expect(amount).toHaveValue(decimal)
      await expect(saved).toContainText(`${currency} ${decimal}`)
      await capture('saved-reloaded')
    })
  }

  await scenario({ name: 'retired-session' }, async (page, state) => {
    // Given
    await page.getByLabel('Fee amount', { exact: true }).fill('0')
    state.putGate = Promise.withResolvers()
    await page.getByRole('button', { name: 'Save transfer policy', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Saving policy…' })).toBeDisabled()
    // When
    await page.getByRole('main').getByRole('button', { name: 'Sign out', exact: true }).click()
    await expect(page).toHaveURL(`${base}login`)
    state.putGate.resolve()
    await state.requests.find((request) => request.method === 'PUT').delivered.promise
    // Then
    await expect(page.getByText('Transfer policy saved', { exact: true })).toHaveCount(0)
    await expect(page.getByLabel('Fee amount', { exact: true })).toHaveCount(0)
  })
} finally {
  await skillBrowser?.close()
  await browser?.close()
  await server.close()
  const receipt = { scenarios, captures, output, cleanup: { closedContexts, browserClosed: true, skillClosed: true, viteClosed: true, syntheticAPIsOnly: true } }
  await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  console.log(JSON.stringify(receipt))
}
