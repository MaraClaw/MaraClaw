// Standalone synthetic QA: node tests/chatgpt-subscription.browser.mjs (from web-a).
// Optional CHATGPT_ARTIFACTS, BROWSER_SKILL_ROOT, CHROMIUM_EXECUTABLE; never opens a provider or installs a browser.
import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'vite'

if (process.env.OMO_BROWSER_ENGINE) throw new Error('Owned synthetic QA requires an unset OMO_BROWSER_ENGINE; do not bypass the session guard.')
const skillRoot = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
const { loadOmowright } = await import(pathToFileURL(join(skillRoot, 'scripts/omowright.mjs')).href)
const { omowright } = await loadOmowright()
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = process.env.CHATGPT_ARTIFACTS ?? await mkdtemp(join(tmpdir(), 'chatgpt-browser-'))
await mkdir(output, { recursive: true })
const base = '/api/enterprise/llm-models/chatgpt-subscription'
const tenantA = 'a8122022-c233-4f10-91df-91e2d065d2cb', tenantB = 'b8122022-c233-4f10-91df-91e2d065d2cb'
const callback = 'http://localhost:1455/auth/callback?code=synthetic-private-code&state=synthetic-state'
const admin = { id: 'operator', identity_id: 'identity', email: 'synthetic@example.test', display_name: 'Synthetic Operator', role: 'org_admin', is_platform_admin: false, tenant_id: tenantA, is_active: true, email_verified: true, must_change_password: false, avatar_url: null, created_at: '2026-10-23T00:00:00Z' }
const companies = [tenantA, tenantB].map((id, i) => ({ id, name: `Synthetic ${i ? 'Beta' : 'Alpha'}`, slug: `synthetic-${i}`, is_active: true, created_at: admin.created_at }))
const receipt = { status: 'running', output, scenarios: [], screenshots: [], controlFrames: [], violations: [], cleanup: { tabs: 0, closedTabs: 0, barriers: 0, released: 0 } }
const inFlight = new Set()
let browser, server, profile, current
const wait = (locator, state = 'visible') => locator.waitFor({ state, timeout: 12_000 })
const button = (page, text) => page.locator('button').filter({ hasText: new RegExp(`^${text}$`, 'u') }).last()
const card = (page) => page.locator('[data-slot="card"]').filter({ has: page.locator('h3').filter({ hasText: /^Connect ChatGPT subscription$/u }) })
const field = (page) => page.getByLabel('Full callback URL', { exact: true })
async function bounded(promise) {
  let timer
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Synthetic barrier timed out')), 12_000) })]) }
  finally { clearTimeout(timer) }
}
function hold(state, path) {
  const entered = Promise.withResolvers(), released = Promise.withResolvers(), done = Promise.withResolvers()
  const barrier = { path, entered: entered.promise, done: done.promise, wait: released.promise, enter: entered.resolve, finish: done.resolve, released: false,
    release() { if (!this.released) { this.released = true; receipt.cleanup.released++; released.resolve() } } }
  state.barriers.push(barrier); receipt.cleanup.barriers++
  return barrier
}
async function api(state, request, response) {
  try {
    assert.ok(state)
    const url = new URL(request.url, 'http://synthetic.invalid'), path = url.pathname
    assert.equal(url.search.includes('callback'), false)
    assert.equal(url.href.includes('synthetic-private-code'), false)
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const payload = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined
    state.calls.push({ path, method: request.method, scope: url.searchParams.get('tenant_id') })
    let status = 200, body
    if (path === `${base}/start`) {
      assert.equal(request.method, 'POST'); assert.equal(payload, undefined)
      assert.equal(url.searchParams.get('tenant_id'), state.platform ? state.scope : null)
      state.attempt++
      body = { attempt_id: `attempt-${state.attempt}`, authorization_url: 'https://auth.example.test/authorize?state=synthetic-state', redirect_uri: 'http://localhost:1455/auth/callback', expires_in: 600 }
    } else if (path === `${base}/complete`) {
      assert.equal(request.method, 'POST'); assert.equal(url.search, '')
      assert.deepEqual(payload, { attempt_id: `attempt-${state.attempt}`, callback_url: callback })
      status = state.completeStatus
      body = status === 200 ? { status: state.outcome, attempt_id: payload.attempt_id, detail: callback } : { detail: callback }
    } else if (path === `${base}/status`) {
      assert.equal(request.method, 'GET'); assert.equal(payload, undefined)
      assert.deepEqual([...url.searchParams], [['attempt_id', `attempt-${state.attempt}`]])
      body = { status: state.recovery, attempt_id: `attempt-${state.attempt}` }
    } else {
      assert.equal(request.method, 'GET')
      switch (path) {
        case '/api/auth/me': body = { ...admin, ...(state.platform ? { role: 'platform_admin', is_platform_admin: true } : {}) }; break
        case '/api/admin/companies': body = companies; break
        case '/api/notifications/unread-count': body = { unread_count: 0 }; break
        case '/api/enterprise/llm-models': case '/api/enterprise/llm-providers': case '/api/admin/search-analytics/orgs': body = []; break
        case '/api/admin/search-analytics/summary': body = { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' }; break
        default: throw new Error(`Blocked unexpected API ${path}`)
      }
    }
    const barrier = state.barriers.find((item) => item.path === path && !item.released)
    if (barrier) { barrier.enter(); await barrier.wait }
    if (!response.destroyed) { response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); response.end(JSON.stringify(body)) }
    barrier?.finish()
  } catch (error) {
    receipt.violations.push(String(error))
    if (!response.destroyed) { response.writeHead(502); response.end('{}') }
  }
}
async function begin(page) {
  await button(page, 'Connect ChatGPT subscription').click()
  await wait(field(page))
  assert.equal(await field(page).evaluate((element) => element === document.activeElement), true)
  assert.deepEqual(await card(page).locator('a').evaluate((element) => ({ target: element.target, rel: element.rel, href: element.href })), { target: '_blank', rel: 'noopener noreferrer', href: 'https://auth.example.test/authorize?state=synthetic-state' })
}
async function submit(page, state) {
  const barrier = hold(state, `${base}/complete`)
  await field(page).fill(callback)
  await button(page, 'Complete ChatGPT sign-in').click()
  await bounded(barrier.entered)
  assert.equal(await field(page).inputValue(), '')
  assert.equal(await field(page).isDisabled(), true)
  assert.equal(await button(page, 'Restart ChatGPT sign-in').isDisabled(), true)
  barrier.release(); await bounded(barrier.done)
}
async function screenshot(page, state, suffix) {
  const controls = await field(page).count() ? [field(page), button(page, 'Complete ChatGPT sign-in'), button(page, 'Restart ChatGPT sign-in')] : []
  const anchor = controls[0] ?? card(page)
  await anchor.evaluate((element) => element.scrollIntoView({ block: 'center', behavior: 'instant' }))
  await page.evaluate(async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame) })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  const bounds = []
  for (const control of controls) {
    const rect = await control.evaluate((element) => {
      const { top, bottom, left, right } = element.getBoundingClientRect()
      return { top, bottom, left, right, viewportWidth: innerWidth, viewportHeight: innerHeight, scrollTop: document.querySelector('main').scrollTop }
    })
    assert.ok(rect.top >= 0 && rect.bottom <= rect.viewportHeight && rect.left >= 0 && rect.right <= rect.viewportWidth, `Callback control outside capture: ${JSON.stringify(rect)}`)
    bounds.push(rect)
  }
  const path = join(output, `${state.name}-${suffix}.png`)
  await writeFile(path, await page.screenshot()); receipt.screenshots.push(path)
  if (bounds.length) receipt.controlFrames.push({ path, bounds })
}
async function scenario(options, exercise) {
  const state = { name: options.name, platform: false, scope: tenantA, width: 1440, theme: 'light', outcome: 'authorized', recovery: 'authorized', completeStatus: 200, attempt: 0, calls: [], barriers: [], ...options }
  current = state
  const tabs = omowright.createAgentTabs(browser), viewport = { width: state.width, height: state.width === 390 ? 844 : 1000 }
  const tab = await tabs.create('about:blank', { viewport }), page = tab.page
  receipt.cleanup.tabs++
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))
  try {
    const session = await page.resolveSessionId()
    await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('maraclaw-admin-token','synthetic-chatgpt-token');localStorage.setItem('maraclaw-admin-theme',${JSON.stringify(state.theme)});` }, session)
    await page.goto(`${server.resolvedUrls.local[0]}models${state.platform ? `?company=${tenantA}` : ''}`)
    await tabs.repin(tab.targetId, viewport)
    if (state.width === 390) await omowright.emulate(page, 'iphone-14')
    await wait(button(page, 'Connect ChatGPT subscription')); await page.snapshot()
    await exercise(page, state)
    assert.deepEqual(pageErrors, [])
    assert.deepEqual(receipt.violations, [])
    assert.equal(state.calls.some((call) => call.path.includes('grok-subscription')), false)
    assert.equal(await page.evaluate(() => [location.href, ...Object.values(localStorage), ...Object.values(sessionStorage), document.body.innerText].some((value) => value.includes('synthetic-private-code'))), false)
    receipt.scenarios.push(state.name)
  } finally {
    for (const barrier of state.barriers) barrier.release()
    await bounded(Promise.all(inFlight))
    await tab.close(); receipt.cleanup.closedTabs++
    current = null
  }
}

try {
  let executable = process.env.CHROMIUM_EXECUTABLE
  if (!executable) { const { chromium } = await import('@playwright/test'); executable = chromium.executablePath() }
  await access(executable)
  profile = await mkdtemp(join(output, 'owned-profile-'))
  const csp = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; connect-src 'self' ws://127.0.0.1:*; object-src 'none'; base-uri 'none'; form-action 'self'; frame-src 'none'"
  server = await createServer({ root, envDir: false, define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') }, cacheDir: join(output, 'vite-cache'), plugins: [{
    name: 'chatgpt-synthetic-firewall', configResolved(config) { config.server.proxy = {} },
    configureServer(vite) { vite.middlewares.use((request, response, next) => {
      if (/^\/api(?:\/|\?|$)/u.test(request.url ?? '')) {
        const work = api(current, request, response)
        inFlight.add(work); void work.finally(() => inFlight.delete(work))
      } else if (!['GET', 'HEAD'].includes(request.method)) { receipt.violations.push('Blocked non-API write'); response.writeHead(502); response.end() }
      else next()
    }) },
  }], server: { host: '127.0.0.1', port: 0, headers: { 'Content-Security-Policy': csp }, watch: { ignored: (path) => path.startsWith(output) } } })
  await server.listen()
  browser = await omowright.connectPipe({ browserPath: executable, browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', `--user-data-dir=${profile}`], storageRoot: profile, commandTimeoutMs: 12_000, dialogPolicy: { accept: false } })
  for (const width of [1440, 390, 1920]) for (const theme of ['light', 'dark']) await scenario({ name: `form-${width}-${theme}`, width, theme }, async (page, state) => {
    // Given: a fresh attempt; starting has not polled status or opened any provider.
    await begin(page)
    assert.equal(state.calls.filter((call) => call.path === `${base}/status`).length, 0)
    await screenshot(page, state, 'callback-controls')
    await button(page, 'Complete ChatGPT sign-in').focus(); await page.keyboard.press('Enter')
    await wait(card(page).locator('[role="alert"]'))
    assert.equal(await field(page).evaluate((element) => element === document.activeElement), true)
    await screenshot(page, state, 'validation')
    // When: completion succeeds through the real UI.
    await submit(page, state)
    // Then: the model pool is refetched and the sensitive form disappears.
    await wait(card(page).locator('[role="status"]')); await wait(field(page), 'detached')
    assert.equal(state.calls.filter((call) => call.path === '/api/enterprise/llm-models').length >= 2, true)
    await screenshot(page, state, 'authorized')
  })
  for (const outcome of ['pending', 'denied', 'expired', 'error', 400, 409, 503]) await scenario({ name: `outcome-${outcome}`, ...(typeof outcome === 'number' ? { completeStatus: outcome } : { outcome }) }, async (page, state) => {
    await begin(page); await submit(page, state)
    if (outcome === 503) { await wait(card(page).locator('[role="status"]')); assert.equal(state.calls.filter((call) => call.path === `${base}/status`).length, 1) }
    else {
      await wait(card(page).locator('[role="alert"]'))
      if (outcome === 400 || outcome === 'pending') {
        await wait(button(page, 'Complete ChatGPT sign-in')); state.completeStatus = 200; state.outcome = 'authorized'
        await submit(page, state); await wait(card(page).locator('[role="status"]'))
      } else {
        await wait(field(page), 'detached'); await button(page, 'Restart ChatGPT sign-in').click(); await wait(field(page))
        assert.equal(state.attempt, 2)
      }
    }
  })
  for (const path of ['start', 'complete']) await scenario({ name: `tenant-switch-${path}`, platform: true }, async (page, state) => {
    if (path === 'complete') await begin(page)
    const barrier = hold(state, `${base}/${path}`)
    if (path === 'start') await button(page, 'Connect ChatGPT subscription').click()
    else { await field(page).fill(callback); await button(page, 'Complete ChatGPT sign-in').click() }
    await bounded(barrier.entered)
    state.scope = tenantB
    await page.locator('main label').filter({ hasText: 'Company' }).locator('select').selectOption(tenantB)
    await wait(button(page, 'Connect ChatGPT subscription'))
    barrier.release(); await bounded(barrier.done)
    await page.evaluate(async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame) })
    await button(page, 'Connect ChatGPT subscription').focus(); await page.keyboard.press('Tab')
    assert.equal(await field(page).count(), 0); assert.equal(await card(page).locator('[role="status"]').count(), 0)
    assert.equal(state.calls.filter((call) => call.path === '/api/enterprise/llm-models' && call.scope === tenantB).length, 1)
  })
  receipt.status = 'passed'
} catch (error) { receipt.status = 'failed'; receipt.error = String(error); process.exitCode = 1 }
finally {
  for (const [key, cleanup] of [
    ['browserClosed', async () => { if (browser) await browser.close() }],
    ['viteClosed', async () => { if (server) await server.close() }],
    ['profileRemoved', async () => { if (profile) await rm(profile, { recursive: true, force: true }) }],
  ]) { try { await cleanup(); receipt.cleanup[key] = true } catch (error) { receipt.cleanup[key] = String(error); receipt.status = 'failed'; process.exitCode = 1 } }
  await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  console.log(JSON.stringify(receipt))
}
