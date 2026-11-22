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
// Provider detail a status answer may carry; the card must never render or store it.
const privateDetail = 'synthetic-private-code'
// AuthUserSchema requires UUID ids; any other value fails the session parse and signs out.
const admin = { id: 'c1a7e5d0-3b6f-4a2e-9d41-7f0c8e2b5a91', identity_id: 'c1a7e5d0-3b6f-4a2e-9d41-7f0c8e2b5a92', email: 'synthetic@example.test', display_name: 'Synthetic Operator', role: 'org_admin', is_platform_admin: false, tenant_id: tenantA, is_active: true, email_verified: true, must_change_password: false, avatar_url: null, created_at: '2026-10-23T00:00:00Z' }
// Gamma is never on the browse page: only the by-id lookup reaches it. The unknown id answers 404.
const tenantC = '28122022-c233-4f10-91df-91e2d065d2cb', unknownTenant = '38122022-c233-4f10-91df-91e2d065d2cb'
const companies = [tenantA, tenantB, tenantC].map((id, i) => ({ id, name: `Synthetic ${['Alpha', 'Beta', 'Gamma'][i]}`, slug: `synthetic-${i}`, is_active: true, can_disable: true, tier: 'pro', created_at: admin.created_at }))
// The card checks status every 2 s and gives up 10 s after the link's 600 s lifetime. The page shim holds exactly
// those timers, so each scenario fires every check itself, and it counts status fetches to prove they never overlap.
const pollMs = 2_000, expiryMs = 610_000
const pageShim = `(() => {
  const buckets = { poll: ${pollMs}, expiry: ${expiryMs} }, held = new Map()
  let nextId = 0
  const hold = (real, repeat) => (callback, delay, ...args) => {
    const bucket = Object.keys(buckets).find((name) => buckets[name] === delay)
    if (!bucket || typeof callback !== 'function') return real(callback, delay, ...args)
    const id = --nextId
    held.set(id, { bucket, repeat, run: () => callback(...args) })
    return id
  }
  const clear = (real) => (id) => { if (!held.delete(id)) real(id) }
  window.setTimeout = hold(window.setTimeout.bind(window), false)
  window.setInterval = hold(window.setInterval.bind(window), true)
  window.clearTimeout = clear(window.clearTimeout.bind(window))
  window.clearInterval = clear(window.clearInterval.bind(window))
  window.__syntheticTimers = {
    count: (bucket) => [...held.values()].filter((item) => item.bucket === bucket).length,
    fire(bucket) {
      const due = [...held].filter(([, item]) => item.bucket === bucket)
      for (const [id, item] of due) { if (!item.repeat) held.delete(id); item.run() }
      return due.length
    },
  }
  // A check is in flight from fetch until its body is read or it is aborted.
  const fetch = window.fetch.bind(window), status = { calls: 0, active: 0, max: 0, settled: 0 }
  window.__statusFetch = status
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (!url.includes('${base}/status')) return fetch(input, init)
    let open = true
    const done = () => { if (open) { open = false; status.active--; status.settled++ } }
    status.calls++; status.active++; status.max = Math.max(status.max, status.active)
    init?.signal?.addEventListener('abort', done, { once: true })
    return fetch(input, init).then((response) => {
      const text = response.text.bind(response)
      response.text = () => text().finally(done)
      return response
    }, (error) => { done(); throw error })
  }
})();`
const receipt = { status: 'running', output, scenarios: [], screenshots: [], controlFrames: [], violations: [], cleanup: { tabs: 0, closedTabs: 0, barriers: 0, released: 0 } }
const inFlight = new Set()
let browser, server, profile, current
const wait = (locator, state = 'visible') => locator.waitFor({ state, timeout: 12_000 })
const button = (page, text) => page.locator('button').filter({ hasText: new RegExp(`^${text}$`, 'u') }).last()
const card = (page) => page.locator('[data-slot="card"]').filter({ has: page.locator('h3').filter({ hasText: /^Connect ChatGPT subscription$/u }) })
const link = (page) => card(page).locator('a[target="_blank"]')
const shownStatus = (page) => card(page).locator('[role="status"]:not(:empty)')
const connect = (page) => page.locator('button:not(:disabled)').filter({ hasText: /^Connect ChatGPT subscription$/u }).last()
const picker = (page) => page.getByLabel('Company', { exact: true })
const selectedCompany = (page) => picker(page).evaluate((select) => ({ value: select.value, label: select.selectedOptions[0]?.textContent, options: [...select.options].map((option) => option.textContent) }))
const focused = (locator) => locator.evaluate((element) => element === document.activeElement)
const statusFetch = (page) => page.evaluate(() => ({ ...window.__statusFetch }))
const fire = (page, bucket) => page.evaluate((name) => window.__syntheticTimers.fire(name), bucket)
const statusCalls = (state) => state.calls.filter((call) => call.path === `${base}/status`).length
const modelCalls = (state, scope = null) => state.calls.filter((call) => call.path === '/api/enterprise/llm-models' && call.scope === scope).length
// One macrotask lets a fired timer's request start, or not, before anything is counted.
const settle = (page) => page.evaluate(() => new Promise((resolve) => { setTimeout(resolve, 0) }))
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
// Bounded in-page waits on shim state, paced by animation frames rather than fixed delays.
async function answered(page, settled) {
  await page.evaluate(async (expected) => {
    const deadline = performance.now() + 12_000
    while (window.__statusFetch.settled < expected || window.__statusFetch.active > 0) {
      if (performance.now() > deadline) throw new Error(`Status check ${expected} did not settle`)
      await new Promise(requestAnimationFrame)
    }
  }, settled)
  await settle(page)
}
async function timersAt(page, bucket, count) {
  await page.evaluate(async ([name, expected]) => {
    const deadline = performance.now() + 12_000
    while (window.__syntheticTimers.count(name) !== expected) {
      if (performance.now() > deadline) throw new Error(`${name} timers did not reach ${expected}`)
      await new Promise(requestAnimationFrame)
    }
  }, [bucket, count])
}
async function api(state, request, response) {
  try {
    assert.ok(state)
    const url = new URL(request.url, 'http://synthetic.invalid'), path = url.pathname
    assert.equal(url.href.includes(privateDetail), false)
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    const payload = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined
    state.calls.push({ path, method: request.method, scope: url.searchParams.get('tenant_id') })
    let status = 200, body
    if (path === `${base}/start`) {
      assert.equal(request.method, 'POST'); assert.equal(payload, undefined)
      assert.equal(url.searchParams.get('tenant_id'), state.platform ? state.scope : null)
      state.attempt++
      body = { attempt_id: `attempt-${state.attempt}`, authorization_url: 'https://auth.example.test/authorize?state=synthetic-state', expires_in: 600 }
    } else if (path === `${base}/status`) {
      assert.equal(request.method, 'GET'); assert.equal(payload, undefined)
      assert.deepEqual([...url.searchParams], [['attempt_id', `attempt-${state.attempt}`]])
      state.statusMax = Math.max(state.statusMax, ++state.statusActive)
      response.once('close', () => { state.statusActive-- })
      // Scripted answers in order: a status name, or an HTTP error code; pending once the script runs out.
      const next = state.statuses.shift() ?? 'pending'
      if (typeof next === 'number') { status = next; body = { detail: privateDetail } }
      else body = { status: next, attempt_id: `attempt-${state.attempt}`, detail: next === 'pending' ? null : privateDetail, expires_in: next === 'pending' ? 600 : null }
    } else if (path.endsWith('/grok-subscription/start')) {
      body = { session_id: 'synthetic-grok', verification_url: 'https://auth.example.test/grok', user_code: 'SYNTHETIC', interval: 5, expires_in: 600 }
    } else if (path.endsWith('/grok-subscription/status')) {
      body = { status: state.outcome, detail: '<html>unsafe provider detail</html>' }
    } else if (path.startsWith('/api/tenants/')) {
      assert.equal(request.method, 'GET')
      const company = companies.find((item) => path === `/api/tenants/${item.id}`)
      if (company) body = company
      else { status = 404; body = { detail: 'Tenant not found' } }
    } else {
      assert.equal(request.method, 'GET')
      switch (path) {
        case '/api/auth/me': body = { ...admin, tenant_id: state.ownTenant ?? admin.tenant_id, ...(state.platform ? { role: 'platform_admin', is_platform_admin: true } : {}) }; break
        case '/api/admin/companies/options': body = { items: companies.slice(0, 2), next_cursor: null }; break
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
// Starting shows the sign-in link (no address field), focuses it and runs the first check at once.
async function begin(page, state, label = 'Connect ChatGPT subscription') {
  const { settled } = await statusFetch(page)
  await button(page, label).click()
  await wait(link(page))
  assert.deepEqual(await link(page).evaluate((element) => ({ target: element.target, rel: element.rel, href: element.href })), { target: '_blank', rel: 'noopener noreferrer', href: 'https://auth.example.test/authorize?state=synthetic-state' })
  assert.equal(await card(page).locator('input, textarea').count(), 0)
  await answered(page, settled + 1)
  assert.equal(await focused(link(page)), true)
  await timersAt(page, 'expiry', 1)
}
// Fires every held poll timer (the interval and any retry back-off) and expects exactly one new check.
async function poll(page) {
  const { calls, settled } = await statusFetch(page)
  assert.ok(await fire(page, 'poll') > 0, 'No status check was scheduled')
  await answered(page, settled + 1)
  assert.equal((await statusFetch(page)).calls, calls + 1)
}
// An outcome removes the link and every timer, sends nothing more and focuses the trigger.
async function ended(page, trigger) {
  await wait(link(page), 'detached')
  await timersAt(page, 'poll', 0); await timersAt(page, 'expiry', 0)
  const { calls } = await statusFetch(page)
  await settle(page)
  assert.equal((await statusFetch(page)).calls, calls)
  assert.equal(await focused(button(page, trigger)), true)
}
async function screenshot(page, state, suffix) {
  const controls = await link(page).count() ? [link(page), button(page, 'Restart ChatGPT sign-in')] : [card(page).locator('button').last()]
  await controls[0].evaluate((element) => element.scrollIntoView({ block: 'center', behavior: 'instant' }))
  await page.evaluate(async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame) })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  const bounds = []
  for (const control of controls) {
    const rect = await control.evaluate((element) => {
      const { top, bottom, left, right } = element.getBoundingClientRect()
      return { top, bottom, left, right, viewportWidth: innerWidth, viewportHeight: innerHeight, scrollTop: document.querySelector('main').scrollTop }
    })
    assert.ok(rect.top >= 0 && rect.bottom <= rect.viewportHeight && rect.left >= 0 && rect.right <= rect.viewportWidth, `Sign-in control outside capture: ${JSON.stringify(rect)}`)
    bounds.push(rect)
  }
  const path = join(output, `${state.name}-${suffix}.png`)
  await writeFile(path, await page.screenshot()); receipt.screenshots.push(path)
  receipt.controlFrames.push({ path, bounds })
}
async function scenario(options, exercise) {
  const state = { name: options.name, platform: false, scope: tenantA, width: 1440, theme: 'light', outcome: 'authorized', statuses: [], statusActive: 0, statusMax: 0, attempt: 0, calls: [], barriers: [], ...options }
  current = state
  const tabs = omowright.createAgentTabs(browser), viewport = { width: state.width, height: state.width === 390 ? 844 : 1000 }
  const tab = await tabs.create('about:blank', { viewport }), page = tab.page
  receipt.cleanup.tabs++
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))
  try {
    const session = await page.resolveSessionId()
    await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `localStorage.setItem('maraclaw-admin-token','synthetic-chatgpt-token');localStorage.setItem('maraclaw-admin-theme',${JSON.stringify(state.theme)});${pageShim}` }, session)
    await page.goto(`${server.resolvedUrls.local[0]}${state.path ?? `models${state.platform ? `?company=${tenantA}` : ''}`}`)
    await tabs.repin(tab.targetId, viewport)
    if (state.width === 390) await omowright.emulate(page, 'iphone-14')
    // A platform admin's company unlocks the card only after its by-id lookup resolves.
    await (state.ready ? state.ready(page) : wait(connect(page))); await page.snapshot()
    await exercise(page, state)
    assert.deepEqual(pageErrors, [])
    assert.deepEqual(receipt.violations, [])
    assert.equal(state.calls.some((call) => call.path.includes('grok-subscription')), Boolean(state.grok))
    // Status checks never overlapped, in the page or at the synthetic server.
    assert.ok((await statusFetch(page)).max <= 1); assert.ok(state.statusMax <= 1)
    assert.equal(await page.evaluate((secret) => [location.href, ...Object.values(localStorage), ...Object.values(sessionStorage), document.body.innerText].some((value) => value.includes(secret)), privateDetail), false)
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
  for (const width of [1440, 390, 1920]) for (const theme of ['light', 'dark']) await scenario({ name: `waiting-${width}-${theme}`, width, theme, statuses: ['pending', 'pending', 'authorized'] }, async (page, state) => {
    // Given: a fresh attempt whose first check answered pending, with the next check scheduled.
    await begin(page, state)
    assert.equal(statusCalls(state), 1)
    await timersAt(page, 'poll', 1)
    await screenshot(page, state, 'waiting')
    // When a check is held, firing every due timer again starts no second request.
    const barrier = hold(state, `${base}/status`)
    await fire(page, 'poll')
    await bounded(barrier.entered)
    assert.equal(await fire(page, 'poll'), 1)
    await settle(page)
    assert.equal(statusCalls(state), 2)
    assert.equal((await statusFetch(page)).active, 1)
    barrier.release(); await bounded(barrier.done)
    await answered(page, 2)
    // Then the next check authorizes: the model pool is refetched and polling stops.
    await poll(page)
    await ended(page, 'Connect ChatGPT subscription')
    await wait(shownStatus(page))
    assert.equal(statusCalls(state), 3)
    assert.ok(modelCalls(state) >= 2)
    await screenshot(page, state, 'authorized')
  })
  for (const outcome of ['denied', 'expired', 'error', 404, 409]) await scenario({ name: `outcome-${outcome}`, statuses: ['pending', outcome] }, async (page, state) => {
    await begin(page, state); await poll(page)
    await ended(page, 'Restart ChatGPT sign-in')
    if (typeof outcome === 'string') {
      // A sign-in outcome is a persistent status, not a toast.
      await wait(shownStatus(page))
      assert.equal(await page.locator('[data-sonner-toast]').count(), 0)
      await screenshot(page, state, 'persistent-status')
    } else await wait(page.locator('[data-sonner-toast]'))
    assert.equal(statusCalls(state), 2)
    // Restart issues a fresh attempt that polls on its own.
    await begin(page, state, 'Restart ChatGPT sign-in')
    assert.equal(state.attempt, 2)
  })
  await scenario({ name: 'transient-failures', statuses: [503, 502, 503, 'pending', 'authorized'] }, async (page, state) => {
    // Given: two failed checks keep the attempt waiting without an error.
    await begin(page, state); await poll(page)
    assert.equal(await page.locator('[data-sonner-toast]').count(), 0)
    assert.equal(await link(page).count(), 1)
    // When a third consecutive check fails, one error appears and checking continues.
    await poll(page)
    await wait(page.locator('[data-sonner-toast]'))
    assert.equal(await link(page).count(), 1)
    await poll(page); await poll(page)
    // Then recovery still authorizes.
    await ended(page, 'Connect ChatGPT subscription')
    assert.ok(modelCalls(state) >= 2)
    assert.ok(await page.locator('[data-sonner-toast]').count() <= 1)
  })
  await scenario({ name: 'local-expiry' }, async (page, state) => {
    // Given: checks keep answering pending.
    await begin(page, state); await poll(page)
    // When the link's lifetime plus grace passes, the card stops waiting without another check.
    assert.equal(await fire(page, 'expiry'), 1)
    await ended(page, 'Restart ChatGPT sign-in')
    await wait(shownStatus(page))
    assert.equal(await page.locator('[data-sonner-toast]').count(), 0)
    assert.equal(statusCalls(state), 2)
  })
  for (const outcome of ['expired', 'denied', 'error']) await scenario({ name: `grok-${outcome}`, outcome, grok: true }, async (page, state) => {
    await button(page, 'Connect Grok subscription').click()
    const grokCard = page.locator('[data-slot="card"]').filter({ has: page.locator('h3').filter({ hasText: /^Connect Grok subscription$/u }) })
    await wait(grokCard.locator('[role="status"]'))
    assert.equal(await page.locator('[data-sonner-toast]').count(), 0)
    assert.equal((await grokCard.textContent()).includes('unsafe provider detail'), false)
    await grokCard.evaluate((element) => element.scrollIntoView({ block: 'center', behavior: 'instant' }))
    await writeFile(join(output, `${state.name}.png`), await page.screenshot())
    receipt.screenshots.push(join(output, `${state.name}.png`))
  })
  for (const path of ['start', 'status']) await scenario({ name: `tenant-switch-${path}`, platform: true, statuses: ['pending', 'authorized'] }, async (page, state) => {
    if (path === 'status') await begin(page, state)
    const barrier = hold(state, `${base}/${path}`)
    if (path === 'start') await button(page, 'Connect ChatGPT subscription').click()
    else await fire(page, 'poll')
    await bounded(barrier.entered)
    const previousModels = modelCalls(state, tenantA)
    state.scope = tenantB
    await picker(page).selectOption(tenantB)
    await wait(connect(page))
    // The old card unmounted: its timers are gone before the held answer (authorized, for status) arrives.
    await timersAt(page, 'poll', 0); await timersAt(page, 'expiry', 0)
    barrier.release(); await bounded(barrier.done)
    await settle(page)
    await page.evaluate(async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame) })
    await button(page, 'Connect ChatGPT subscription').focus(); await page.keyboard.press('Tab')
    assert.equal(await link(page).count(), 0); assert.equal(await shownStatus(page).count(), 0)
    assert.equal(modelCalls(state, tenantB), 1); assert.equal(modelCalls(state, tenantA), previousModels)
    assert.equal(statusCalls(state), path === 'status' ? 2 : 0)
    assert.equal((await statusFetch(page)).active, 0)
  })
  const lookups = (state) => [...new Set(state.calls.filter((call) => call.path.startsWith('/api/tenants/')).map((call) => call.path))]
  const modelScopes = (state) => [...new Set(state.calls.filter((call) => call.path === '/api/enterprise/llm-models').map((call) => call.scope))]
  const companyParam = (page) => page.evaluate(() => new URL(location.href).searchParams.get('company'))
  for (const [name, ownTenant, expected] of [['pa-own-company-preselect', tenantC, tenantC], ['pa-own-company-lookup-fallback', unknownTenant, tenantA]]) {
    await scenario({ name, platform: true, ownTenant, scope: expected, path: 'models' }, async (page, state) => {
      assert.deepEqual(await selectedCompany(page), { value: expected, label: expected === tenantC ? 'Synthetic Gamma' : 'Synthetic Alpha', options: expected === tenantC ? ['Synthetic Gamma', 'Synthetic Alpha', 'Synthetic Beta'] : ['Synthetic Alpha', 'Synthetic Beta'] })
      assert.equal(await companyParam(page), expected)
      assert.deepEqual(modelScopes(state), [expected])
      assert.equal(lookups(state).includes(`/api/tenants/${ownTenant}`), true)
      await begin(page, state)
    })
  }
  await scenario({ name: 'pa-off-page-deep-link', platform: true, scope: tenantC, path: `models?company=${tenantC}` }, async (page, state) => {
    assert.deepEqual(await selectedCompany(page), { value: tenantC, label: 'Synthetic Gamma', options: ['Synthetic Gamma', 'Synthetic Alpha', 'Synthetic Beta'] })
    assert.deepEqual(lookups(state), [`/api/tenants/${tenantC}`])
    assert.deepEqual(modelScopes(state), [tenantC])
    await begin(page, state)
  })
  await scenario({ name: 'pa-unknown-deep-link', platform: true, scope: unknownTenant, path: `models?company=${unknownTenant}`, ready: (page) => wait(page.locator('option').filter({ hasText: /^Unknown company$/u }), 'attached') }, async (page, state) => {
    assert.deepEqual(await selectedCompany(page), { value: unknownTenant, label: 'Unknown company', options: ['Unknown company', 'Synthetic Alpha', 'Synthetic Beta'] })
    assert.equal(await companyParam(page), unknownTenant)
    assert.equal(await button(page, 'Connect ChatGPT subscription').isDisabled(), true)
    assert.deepEqual(modelScopes(state), [])
    assert.equal(state.calls.some((call) => call.method !== 'GET'), false)
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
