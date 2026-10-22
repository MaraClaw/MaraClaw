/**
 * Standalone, synthetic-only Tools QA. Run from web-a: node tests/tools.browser.mjs
 * Optional: TOOLS_ARTIFACTS=/owned/output CHROMIUM_EXECUTABLE=/existing/chromium.
 * No backend, credentials, installs, Playwright launches, or user browser profiles.
 * UI labels mirror pages/tools*.tsx. All network contracts below are synthetic.
 */
// allow: SIZE_OK — standalone QA keeps synthetic route guards and owned-browser cleanup in one executable.
import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { homedir, tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createServer } from 'vite'
const browserSkillRoot = process.env.BROWSER_SKILL_ROOT ?? join(homedir(), '.config/oh-my-openagent/dist/skills/browser')
const { loadOmowright } = await import(pathToFileURL(join(browserSkillRoot, 'scripts/omowright.mjs')).href)

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const output = process.env.TOOLS_ARTIFACTS ?? await mkdtemp(join(tmpdir(), 'tools-browser-'))
await mkdir(output, { recursive: true })
const ui = {
  add: 'Create MCP tool', create: 'Create MCP tool', save: 'Save details', cancel: 'Cancel',
  name: 'Tool name', display: 'Display name', description: 'Description',
  config: 'Complete replacement configuration (JSON object)', company: 'Company context',
  confirmConfig: 'I understand this replaces all previous configuration and removes omitted values.',
  saveConfig: 'Replace entire configuration', confirmDelete: 'Delete tool permanently',
  retry: 'Retry loading tools', empty: 'No tools in this catalog',
  edit: (name) => `Edit details for ${name}`, remove: (name) => `Delete tool ${name}`,
  replace: (name) => `Replace configuration for ${name}`,
}
const tenantA = 'a8122022-c233-4f10-91df-91e2d065d2cb'
const tenantB = 'b8122022-c233-4f10-91df-91e2d065d2cb'
const builtinId = 'c8122022-c233-4f10-91df-91e2d065d2cb'
const customId = 'd8122022-c233-4f10-91df-91e2d065d2cb'
const createdId = 'e8122022-c233-4f10-91df-91e2d065d2cb'
const globalId = 'f8122022-c233-4f10-91df-91e2d065d2cb'
const foreignId = '18122022-c233-4f10-91df-91e2d065d2cb'
const longLabel = 'Synthetic international operations connector — LongUnbrokenToolLabelForNarrowScreenReflow0123456789'
const builtin = {
  id: builtinId, name: 'synthetic_builtin', display_name: 'Synthetic built-in lookup',
  description: 'Shared metadata; company-specific source configuration', type: 'builtin', category: 'search', icon: 'search',
  enabled: true, is_default: true, source: 'builtin', parameters_schema: { type: 'object', properties: {} },
  tenant_id: null, capabilities: { can_edit_details: false, can_delete: false, can_configure: true },
  mcp_server_url: null, mcp_server_name: null, mcp_tool_name: null, config: {}, config_schema: {},
  created_at: '2026-10-20T00:00:00Z',
}
const custom = {
  ...builtin, id: customId, name: 'synthetic_connector', display_name: longLabel,
  description: 'Synthetic only. Long content exercises wrapping, editor scrolling and destructive confirmation.',
  type: 'mcp', category: 'integration', is_default: false, source: 'admin',
  tenant_id: tenantA, capabilities: { can_edit_details: true, can_delete: true, can_configure: true },
  mcp_server_url: 'https://connector.example.test/mcp', mcp_server_name: 'synthetic', mcp_tool_name: 'lookup',
  config: { api_key: '********', region: 'eu-west-1', legacy: 'remove-me' },
  config_schema: { fields: [{ key: 'api_key', type: 'password' }] },
}
const globalTool = { ...custom, id: globalId, name: 'synthetic_global', display_name: 'Synthetic global connector', tenant_id: null }
const foreignTool = { ...custom, id: foreignId, name: 'synthetic_beta', display_name: 'Synthetic Beta connector', tenant_id: tenantB }
function catalogFixture(tool, { role, scope }) {
  const platform = role === 'platform_admin'
  const resolvedTenant = scope === 'current' ? admin.tenant_id : scope
  const own = tool.source !== 'builtin' && tool.type !== 'builtin' && tool.tenant_id !== null && tool.tenant_id === tenantA
  return { ...tool, capabilities: {
    can_edit_details: platform || own,
    can_delete: tool.type !== 'builtin' && (platform || own),
    can_configure: tool.source === 'builtin' ? !!resolvedTenant && (platform || scope === tenantA) : platform || own,
  } }
}
function catalogFixtures(role) {
  const scopes = { current: [builtin, custom, globalTool, foreignTool], [tenantA]: [builtin, custom, globalTool, foreignTool], [tenantB]: [builtin, foreignTool, globalTool] }
  return Object.fromEntries(Object.entries(scopes).map(([scope, rows]) => [scope, structuredClone(rows.map((tool) => catalogFixture(tool, { role, scope })))]))
}
const admin = {
  id: '66dccce3-bcfb-40ef-b334-49c2b47d614d', identity_id: '66dccce3-bcfb-40ef-b334-49c2b47d614d',
  email: 'tools-operator@example.test', display_name: 'Synthetic Tools Operator', avatar_url: null,
  role: 'org_admin', is_platform_admin: false, tenant_id: tenantA, is_active: true,
  email_verified: true, must_change_password: false, created_at: builtin.created_at,
}
const companies = [tenantA, tenantB].map((id, index) => ({ id, name: `Synthetic ${index ? 'Beta' : 'Alpha'}`, slug: `synthetic-${index}`, is_active: true, created_at: builtin.created_at }))
const matrix = [1440, 1920, 390].flatMap((width) => ['light', 'dark'].map((theme) => ({ width, theme })))
const receipt = {
  startedAt: new Date().toISOString(), status: 'running', engine: 'omowright.connectPipe',
  output, scenarios: [], assertions: [], screenshots: [], requests: [], violations: [], findings: [], blockedAssets: [], labels: ui,
  transport: { api: 'owned Vite middleware; no CDP Fetch routes', proxyDisabled: false, abandonedResponses: 0, csp: 'External origins forbidden; local dev/automation evaluation allowed. First CSP run proved local eval was blocked (not a network escape); no violation was ignored.' },
  fixtureDisclosure: 'OA foreign-company row is a deliberate negative capability-presentation fixture beyond real catalog visibility; all records and secrets are synthetic.',
  plannedMatrix: matrix, cleanup: { routesCreated: 0, routesDisposed: 0, apiRoutesCreated: 0, apiRoutesDisposed: 0, barriersCreated: 0, barriersReleased: 0, tabsCreated: 0, tabsClosed: 0, browserClosed: false, profileRemoved: false, viteClosed: false },
}
const { omowright } = await loadOmowright()
let browser, server, profile
let activeScenario = 'setup'
// Scenarios are serial. Capture this state once per HTTP request, never at release time.
let activeScenarioState = null
const inFlightApi = new Set()
const verify = (name, actual, expected = true) => { assert.deepEqual(actual, expected, name); receipt.assertions.push({ scenario: activeScenario, name }) }
// CSS + exact text avoids this installed omowright version's implicit-role resolver
// timing out even when snapshot() positively identifies the same heading/button.
const exactText = (text) => new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&')}$`, 'u')
const button = (page, name) => /^(?:Edit details for |Replace configuration for |Delete tool )/u.test(name) && name !== ui.confirmDelete
  ? page.locator(`button[aria-label=${JSON.stringify(name)}]`)
  : page.locator('button:not([aria-label])').filter({ hasText: exactText(name) }).last()
const field = (page, name) => page.getByLabel(name, { exact: true })
const visible = (locator) => locator.waitFor({ state: 'visible', timeout: 12_000 })
const detached = (locator) => locator.waitFor({ state: 'detached', timeout: 12_000 })
const mutations = (state) => state.calls.filter((call) => call.method !== 'GET')

function responseBarrier(state, match) {
  const entered = Promise.withResolvers(), released = Promise.withResolvers(), completed = Promise.withResolvers()
  const barrier = { match, entered: entered.promise, completed: completed.promise, wait: released.promise, requests: [], released: false,
    enter(request) { this.requests.push(request); entered.resolve(request) },
    complete() { completed.resolve() },
    release() { if (!this.released) { this.released = true; receipt.cleanup.barriersReleased++; released.resolve() } },
  }
  state.barriers.push(barrier)
  receipt.cleanup.barriersCreated++
  return barrier
}

async function bounded(promise, message) {
  let timer
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), 12_000) })]) }
  finally { clearTimeout(timer) }
}

// Contract fixture: PUT config is replacement, never a merge or a mask echo.
function toolsResponse(state, request) {
  const { method, path, params, payload } = request
  const selected = payload?.tenant_id ?? params.tenant_id
  // DELETE has no tenant parameter; the fixture server still enforces record capabilities.
  const scope = method === 'DELETE' ? state.scope : state.role === 'platform_admin' ? selected ?? 'current' : tenantA
  verify('every Tools call is bound to the selected tenant', scope === state.scope)
  if (state.role === 'org_admin') verify('OA cannot request another tenant', !selected || selected === tenantA)
  const rows = state.catalog[scope]
  assert.ok(rows, `Unknown synthetic tenant ${scope}`)
  if (method === 'GET') {
    assert.equal(path, '/api/tools', 'Only the documented list route is supported')
    const masked = rows.map((tool) => ({ ...catalogFixture(tool, state), config: Object.fromEntries(Object.entries(tool.config).map(([key, value]) => [key, /key|token|secret|password/iu.test(key) ? '********' : value])) }))
    return { status: state.status, body: state.status === 200 ? masked : { detail: 'Synthetic tools service unavailable' } }
  }
  verify('masked config is never submitted', !/[*•]{3,}/u.test(JSON.stringify(payload)))
  const id = path.split('/').at(-1)
  if (method !== 'POST') {
    const tool = rows.find((row) => row.id === id)
    assert.ok(tool, 'Mutation targets an existing fixture')
    const { capabilities } = catalogFixture(tool, state)
    verify('mutation uses a server-permitted capability', method === 'DELETE' ? capabilities.can_delete : Object.hasOwn(payload, 'config') ? capabilities.can_configure : capabilities.can_edit_details)
  }
  if (state.mutationStatus === 403) {
    state.rejectedWrites++
    return { status: 403, body: { detail: 'Synthetic permissions changed; write denied' } }
  }
  switch (method) {
    case 'POST':
      assert.equal(path, '/api/tools')
      state.catalog[scope] = [...rows, catalogFixture({ ...custom, ...payload, tenant_id: scope, id: createdId, config: {} }, state)]
      return { status: 200, body: { id: createdId, name: payload.name } }
    case 'PUT':
      assert.ok(rows.some((tool) => tool.id === id), 'Update targets an existing fixture')
      // Non-built-in metadata/config are shared record writes; built-in config is scoped.
      for (const key of Object.keys(state.catalog)) if (key === scope || rows.find((tool) => tool.id === id).type !== 'builtin') {
        state.catalog[key] = state.catalog[key].map((tool) => tool.id === id ? { ...tool, ...Object.fromEntries(Object.entries(payload).filter(([key]) => key !== 'tenant_id')) } : tool)
      }
      return { status: 200, body: { ok: true } }
    case 'DELETE':
      assert.ok(rows.some((tool) => tool.id === id), 'Delete targets an existing fixture')
      for (const key of Object.keys(state.catalog)) state.catalog[key] = state.catalog[key].filter((tool) => tool.id !== id)
      return { status: 200, body: { ok: true } }
    default: throw new Error(`Unexpected Tools method ${method}`)
  }
}

async function syntheticApi(state, incoming, outgoing) {
  const url = new URL(incoming.url, 'http://synthetic.invalid')
  const path = url.pathname.replace(/\/$/u, '')
  const scenarioName = state?.name ?? 'no-active-scenario'
  try {
    assert.ok(state, 'API requests require an active synthetic scenario')
    const requestState = { ...state, scope: state.scope }
    const chunks = []
    for await (const chunk of incoming) chunks.push(chunk)
    const body = Buffer.concat(chunks).toString('utf8')
    const request = { method: incoming.method, path, params: Object.fromEntries(url.searchParams), payload: body ? JSON.parse(body) : undefined }
    receipt.requests.push({ scenario: scenarioName, ...request })
    const tools = /^\/api\/tools(?:\/[^/]+)?$/u.test(path)
    const ancillary = ['/api/auth/me', '/api/notifications/unread-count', '/api/admin/companies', '/api/admin/search-analytics/summary', '/api/admin/search-analytics/orgs']
    assert.ok(tools || ancillary.includes(path), `Blocked unrecognized API ${path}`)
    if (!tools) assert.equal(request.method, 'GET', 'Only Tools mutations are allowed')
    else state.calls.push(request)
    const barrier = state.barriers.find((item) => !item.released && item.match(request))
    if (barrier) { barrier.enter(request); await barrier.wait }
    let response
    switch (path) {
      case '/api/auth/me': response = { body: { ...admin, role: state.role, is_platform_admin: state.role === 'platform_admin' } }; break
      case '/api/notifications/unread-count': response = { body: { unread_count: 0 } }; break
      case '/api/admin/companies': response = { body: companies }; break
      case '/api/admin/search-analytics/summary': response = { body: { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' } }; break
      case '/api/admin/search-analytics/orgs': response = { body: [] }; break
      default:
        response = toolsResponse(requestState, request)
        state.rejectedWrites = requestState.rejectedWrites
    }
    // An HTTP client abort is not a failed CDP fulfillment: no interception ID exists.
    if (outgoing.destroyed) receipt.transport.abandonedResponses++
    else { outgoing.writeHead(response.status ?? 200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); outgoing.end(JSON.stringify(response.body)) }
    barrier?.complete()
  } catch (error) {
    receipt.violations.push({ scenario: scenarioName, path, message: String(error) })
    if (!outgoing.destroyed) { outgoing.writeHead(502, { 'Content-Type': 'application/json' }); outgoing.end('{"detail":"Blocked by synthetic API firewall"}') }
  }
}

async function capture(page, state) {
  const dimensions = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, scale: devicePixelRatio, touch: navigator.maxTouchPoints, ua: navigator.userAgent, dark: document.documentElement.classList.contains('dark'), documentFits: document.documentElement.scrollWidth <= innerWidth, mainFits: [...document.querySelectorAll('main')].every((main) => main.scrollWidth <= main.clientWidth), focus: { tag: document.activeElement?.tagName, name: document.activeElement?.getAttribute('aria-label'), text: document.activeElement?.textContent?.trim().slice(0, 120) } }))
  verify('viewport width is pinned', dimensions.width, state.width)
  verify('requested theme is active', dimensions.dark, state.theme === 'dark')
  for (const [name, fits] of [['document', dimensions.documentFits], ['main', dimensions.mainFits]]) {
    if (!fits) receipt.findings.push({ scenario: activeScenario, message: `${name} has horizontal overflow` })
    else verify(`${name} has no horizontal overflow`, fits)
  }
  if (state.width === 390) { verify('true iPhone touch emulation', dimensions.touch > 0); verify('iPhone user agent', /iPhone/u.test(dimensions.ua)); verify('iPhone pixel ratio', dimensions.scale, 3) }
  const path = join(output, `${activeScenario}.png`)
  await writeFile(path, await page.screenshot())
  receipt.screenshots.push({ scenario: activeScenario, path, ...dimensions })
}

async function scenario(options, exercise) {
  activeScenario = options.name
  console.log(`START ${options.name}`)
  const state = { role: 'org_admin', scope: tenantA, width: 1440, theme: 'light', status: 200, mutationStatus: 200, rejectedWrites: 0, calls: [], barriers: [], catalog: catalogFixtures(options.role ?? 'org_admin'), ...options, origin: new URL(server.resolvedUrls.local[0]).origin }
  activeScenarioState = state
  if (state.loading) state.loadingBarrier = responseBarrier(state, (request) => request.method === 'GET' && request.path === '/api/tools')
  // Use the supported owned-tab manager with per-tab dimensions. Drain its
  // navigation repin before applying iPhone metrics (repin itself is desktop-only).
  const tabs = omowright.createAgentTabs(browser)
  const viewport = { width: state.width, height: state.width === 390 ? 844 : 1000 }
  const tab = await tabs.create('about:blank', { viewport })
  const page = tab.page
  receipt.cleanup.tabsCreated++
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))
  try {
    await omowright.emulate(page, state.width === 390 ? 'iphone-14' : { width: state.width, height: 1000, deviceScaleFactor: 1, mobile: false, hasTouch: false })
    const session = await page.resolveSessionId()
    await page.cdp.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: state.theme }, { name: 'prefers-reduced-motion', value: 'reduce' }] }, session)
    await page.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `window.__toolsCsp=[];document.addEventListener('securitypolicyviolation',event=>window.__toolsCsp.push({uri:event.blockedURI,directive:event.effectiveDirective,disposition:event.disposition}));localStorage.setItem('maraclaw-admin-token','synthetic-tools-token');localStorage.setItem('maraclaw-admin-theme',${JSON.stringify(state.theme)});` }, session)
    await page.goto(`${state.origin}/tools`)
    await tabs.repin(tab.targetId, viewport)
    if (state.width === 390) await omowright.emulate(page, 'iphone-14')
    await visible(page.locator('main'))
    await page.snapshot()
    await visible(page.locator('main h1, main h2, main h3').filter({ hasText: /^Tools$/u }))
    await exercise(page, state)
    const policyEvents = await page.evaluate(() => window.__toolsCsp)
    for (const event of policyEvents) {
      // The only expected external asset is the known Google Fonts CSS import.
      // Enforced CSP blocks it before network; installed fallback fonts are intentional.
      if (event.disposition === 'enforce' && event.directive === 'style-src-elem' && event.uri.startsWith('https://fonts.googleapis.com/')) receipt.blockedAssets.push({ scenario: state.name, ...event })
      else receipt.violations.push({ scenario: state.name, message: 'Unexpected CSP boundary attempt', ...event })
    }
    verify('no uncaught page errors', pageErrors, [])
    verify('all requests stayed inside synthetic boundaries', receipt.violations, [])
    receipt.scenarios.push({ name: options.name, status: 'passed' })
    console.log(`PASS ${options.name}`)
  } catch (error) {
    receipt.failure = { scenario: activeScenario, message: String(error), snapshot: omowright.compactSnapshot(await page.snapshot()) }
    await writeFile(join(output, 'failure.png'), await page.screenshot())
    throw error
  } finally {
    for (const barrier of state.barriers) barrier.release()
    await bounded(Promise.all(inFlightApi), 'Owned API did not drain during scenario cleanup')
    // Close directly while the Vite firewall remains live. Navigating about:blank
    // first schedules the manager's asynchronous repin against a closing CDP session.
    await tab.close(); receipt.cleanup.tabsClosed++
    activeScenarioState = null
    await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  }
}

async function assertFocus(page, locator) {
  verify('keyboard focus is on the expected control', await locator.evaluate((element) => element === document.activeElement))
  await page.keyboard.press('Tab')
  verify('Tab reaches a visible focus target', await page.evaluate(() => { const e = document.activeElement; return e !== document.body && e.getBoundingClientRect().height > 0 }))
}

async function selectCompany(page, state, scope) {
  const previousHeading = await page.locator('main h2').filter({ hasText: exactText('Tool catalog') }).evaluate((element) => element.id)
  state.scope = scope
  await field(page, ui.company).selectOption(scope)
  await detached(page.locator(`[id=${JSON.stringify(previousHeading)}]`))
  await visible(page.locator('button:not(:disabled):not([aria-label])').filter({ hasText: exactText('Refresh catalog') }))
}

async function assertDeniedControl(locator) {
  verify('denied control is disabled, described visibly and retains a touch-sized target', await locator.evaluate((element) => {
    const descriptions = (element.getAttribute('aria-describedby') ?? '').split(/\s+/u).filter(Boolean).map((id) => document.getElementById(id))
    const rect = element.getBoundingClientRect()
    return { disabled: element.disabled, described: descriptions.length > 0 && descriptions.every((description) => description?.textContent.trim() && description.getBoundingClientRect().height > 0), touchTarget: rect.height >= 44 && rect.width >= 44 }
  }), { disabled: true, described: true, touchTarget: true })
}

async function assertOwnership(page, tool, expected) {
  const actual = await button(page, ui.edit(tool.display_name)).evaluate((element) => Object.fromEntries([...element.closest('[data-slot="card"]').querySelectorAll('dt')].map((term) => [term.textContent, term.nextElementSibling.textContent])))
  verify('ownership label follows the record rather than selected scope', actual.Ownership, expected)
  verify('company ID identifies the actual owner or remains absent for shared records', actual['Company ID'] ?? null, tool.tenant_id)
}

async function assertOrgCapabilities(page) {
  await assertOwnership(page, builtin, 'Built-in')
  await assertOwnership(page, custom, 'Company')
  await assertOwnership(page, globalTool, 'Global')
  await assertOwnership(page, foreignTool, 'Company')
  await assertDeniedControl(button(page, ui.edit(builtin.display_name)))
  verify('OA may configure built-in source for its company', await button(page, ui.replace(builtin.display_name)).isDisabled(), false)
  verify('builtin delete stays hidden', await button(page, ui.remove(builtin.display_name)).count(), 0)
  for (const tool of [globalTool, foreignTool]) for (const action of [ui.edit, ui.replace, ui.remove]) await assertDeniedControl(button(page, action(tool.display_name)))
  for (const action of [ui.edit, ui.replace, ui.remove]) verify('OA own MCP action is enabled', await button(page, action(custom.display_name)).isDisabled(), false)
}

async function exerciseCrud(page, state) {
  // Given: OA sees its own MCP, shared built-in/global records and a foreign company record.
  await visible(button(page, ui.edit(longLabel)))
  await assertOrgCapabilities(page)
  const beforeDenied = mutations(state).length
  for (const tool of [globalTool, foreignTool]) for (const action of [ui.edit, ui.replace, ui.remove]) await button(page, action(tool.display_name)).evaluate((element) => element.click())
  verify('shared and foreign read-only actions never write', mutations(state).length, beforeDenied)
  // When: keyboard opens and cancels the inline editor (not a modal/Escape contract).
  const edit = button(page, ui.edit(longLabel))
  await edit.focus(); await page.keyboard.press('Enter')
  await visible(field(page, ui.display))
  await button(page, ui.cancel).focus(); await page.keyboard.press('Enter')
  await detached(field(page, ui.display))
  verify('Cancel restores editor trigger focus', await edit.evaluate((element) => element === document.activeElement))
  verify('cancel does not write', mutations(state).length, 0)
  // When: permissions change after the catalog was loaded and the server rejects a write.
  state.mutationStatus = 403
  await edit.click(); await field(page, ui.display).fill('Synthetic rejected edit')
  await button(page, ui.save).click(); await visible(page.locator('main [role="alert"]'))
  verify('403 rejection reaches the inline error state', state.rejectedWrites, 1)
  verify('403 keeps the entered metadata available', await field(page, ui.display).inputValue(), 'Synthetic rejected edit')
  verify('403 does not mutate the catalog', state.catalog[tenantA].find((tool) => tool.id === customId).display_name, longLabel)
  verify('403 does not clear the authenticated session', await page.evaluate(() => localStorage.getItem('maraclaw-admin-token')), 'synthetic-tools-token')
  await button(page, ui.cancel).click(); await detached(field(page, ui.display))
  state.mutationStatus = 200
  // When: metadata is saved without opting into configuration replacement.
  await edit.click(); await field(page, ui.display).fill('Synthetic renamed connector')
  await button(page, ui.save).click(); await visible(button(page, ui.edit('Synthetic renamed connector')))
  verify('metadata edit omits masked configuration', Object.hasOwn(mutations(state).at(-1).payload, 'config'), false)
  // When: a synthetic new secret explicitly replaces the whole config.
  await button(page, ui.replace('Synthetic renamed connector')).click()
  verify('configuration draft excludes mask values', !/[*•]{3,}/u.test(await field(page, ui.config).inputValue()))
  const replacement = { api_key: 'synthetic-replacement-not-a-real-secret', region: 'ap-southeast-1' }
  await field(page, ui.confirmConfig).check()
  const beforeMask = mutations(state).length
  await field(page, ui.config).fill(JSON.stringify({ api_key: '********' })); await button(page, ui.saveConfig).click()
  await visible(page.locator('[role="alert"]').filter({ hasText: /Masked values cannot be saved/u }))
  verify('masked draft is rejected before network submission', mutations(state).length, beforeMask)
  await field(page, ui.config).fill(JSON.stringify(replacement)); await button(page, ui.saveConfig).click()
  await detached(field(page, ui.config))
  verify('replacement is exact, not merged with masked or legacy fields', mutations(state).at(-1).payload.config, replacement)
  // When: OA replaces only the built-in source configuration for its own company.
  await button(page, ui.replace(builtin.display_name)).click()
  await field(page, ui.config).fill('{"region":"synthetic-company-region"}')
  await field(page, ui.confirmConfig).check(); await button(page, ui.saveConfig).click()
  await detached(field(page, ui.config))
  verify('built-in configuration write uses company scope without metadata', mutations(state).at(-1), { method: 'PUT', path: `/api/tools/${builtinId}`, params: {}, payload: { config: { region: 'synthetic-company-region' }, tenant_id: tenantA } })
  verify('built-in configuration does not change shared ownership', state.catalog[tenantA].find((tool) => tool.id === builtinId).tenant_id, null)
  // When: a new custom tool is created and deleted through confirmation.
  await button(page, ui.add).click()
  await field(page, ui.name).fill('synthetic_created')
  await field(page, ui.display).fill('Synthetic created connector')
  await field(page, ui.description).fill('Synthetic test tool; no real integration')
  await field(page, 'MCP server URL').fill('https://created.example.test/mcp')
  await field(page, 'MCP server name').fill('synthetic-created')
  await field(page, 'MCP tool name').fill('lookup')
  await button(page, ui.create).click(); await visible(button(page, ui.remove('Synthetic created connector')))
  await button(page, ui.remove('Synthetic created connector')).click()
  const before = mutations(state).length
  await button(page, ui.cancel).click()
  verify('delete cancellation never writes', mutations(state).length, before)
  await button(page, ui.remove('Synthetic created connector')).click()
  await button(page, ui.confirmDelete).click(); await detached(button(page, ui.remove('Synthetic created connector')))
  verify('one confirmed deletion', mutations(state).filter((call) => call.method === 'DELETE').length, 1)
  verify('create submitted the input name', mutations(state).find((call) => call.method === 'POST').payload.name, 'synthetic_created')
}

async function captureMatrix(page, state) {
  await visible(button(page, ui.edit(longLabel)))
  await assertOrgCapabilities(page)
  await capture(page, state)
  activeScenario = `${state.name}-catalog-bottom`
  await button(page, ui.edit(foreignTool.display_name)).evaluate((element) => element.scrollIntoView({ block: 'center', behavior: 'instant' }))
  await assertInViewport(button(page, ui.edit(foreignTool.display_name)), 'catalog bottom capture reaches the last record, independently of form-end proof')
  await capture(page, state)
  for (const panel of ['editor', 'config', 'delete']) {
    activeScenario = `${state.name}-${panel}`
    await button(page, panel === 'editor' ? ui.edit(longLabel) : panel === 'config' ? ui.replace(longLabel) : ui.remove(longLabel)).click()
    const target = button(page, panel === 'editor' ? ui.save : panel === 'config' ? ui.saveConfig : ui.confirmDelete)
    await visible(target)
    await capture(page, state)
    await target.focus(); await assertFocus(page, target)
    await button(page, ui.cancel).evaluate((element) => element.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' }))
    activeScenario += '-scrolled-end-keyboard'
    await assertInViewport(button(page, ui.cancel), 'form Cancel is actually inside the viewport')
    await assertInViewport(target, 'form submit/action row is actually inside the viewport')
    await capture(page, state)
    await button(page, ui.cancel).click()
  }
}

async function assertInViewport(locator, name) {
  const bounds = await locator.evaluate((element) => {
    const rect = element.getBoundingClientRect()
    return { width: rect.width, height: rect.height, top: rect.top, left: rect.left, bottom: rect.bottom, right: rect.right, viewportWidth: innerWidth, viewportHeight: innerHeight }
  })
  assert.ok(bounds.width > 0 && bounds.height > 0 && bounds.top >= 0 && bounds.left >= 0 && bounds.bottom <= bounds.viewportHeight && bounds.right <= bounds.viewportWidth, `${name}: ${JSON.stringify(bounds)}`)
  receipt.assertions.push({ scenario: activeScenario, name, bounds })
}

async function exercisePendingWrites(page, state) {
  let label = longLabel
  for (const kind of ['metadata', 'config', 'delete']) {
    await visible(button(page, ui.edit(label)))
    await button(page, kind === 'metadata' ? ui.edit(label) : kind === 'config' ? ui.replace(label) : ui.remove(label)).click()
    if (kind === 'metadata') await field(page, ui.display).fill('Synthetic pending write accepted')
    if (kind === 'config') {
      await field(page, ui.config).fill('{"api_key":"synthetic-pending-secret","region":"pending-region"}')
      await field(page, ui.confirmConfig).check()
    }
    const barrier = responseBarrier(state, (request) => request.path === `/api/tools/${customId}` && request.method === (kind === 'delete' ? 'DELETE' : 'PUT'))
    await button(page, kind === 'metadata' ? ui.save : kind === 'config' ? ui.saveConfig : ui.confirmDelete).click()
    await bounded(barrier.entered, `${kind} mutation did not reach the response barrier`)
    verify(`${kind}: Cancel is disabled while the server response is held`, await button(page, ui.cancel).isDisabled())
    const pending = button(page, kind === 'metadata' ? 'Saving…' : kind === 'config' ? 'Replacing…' : 'Deleting…')
    verify(`${kind}: pending submit is disabled`, await pending.isDisabled())
    verify(`${kind}: mutation has not committed before barrier release`, state.catalog[tenantA].find((tool) => tool.id === customId).display_name, label)
    activeScenario = `${state.name}-${kind}-pending`
    await button(page, ui.cancel).evaluate((element) => element.scrollIntoView({ block: 'center', behavior: 'instant' }))
    await assertInViewport(button(page, ui.cancel), `${kind}: disabled Cancel is visible`)
    await capture(page, state)
    const beforeLists = state.calls.filter((request) => request.method === 'GET').length
    barrier.release()
    await bounded(barrier.completed, `${kind} mutation did not complete`)
    await detached(button(page, ui.cancel))
    if (kind === 'metadata') { label = 'Synthetic pending write accepted'; await visible(button(page, ui.edit(label))) }
    else if (kind === 'delete') await detached(button(page, ui.edit(label)))
    else {
      await visible(button(page, ui.edit(label)))
      await button(page, ui.replace(label)).click()
      verify('accepted config is reflected by the refreshed real editor', JSON.parse(await field(page, ui.config).inputValue()), { region: 'pending-region' })
      await button(page, ui.cancel).click(); await detached(field(page, ui.config))
    }
    verify(`${kind}: accepted write refetched the catalog`, state.calls.filter((request) => request.method === 'GET').length > beforeLists)
  }
}

async function exerciseTenantSwitchPending(page, state) {
  await visible(field(page, ui.company))
  await selectCompany(page, state, tenantA)
  await selectCompany(page, state, tenantB) // Prime the other shared-record cache.
  await selectCompany(page, state, tenantA)
  await button(page, ui.edit(globalTool.display_name)).click()
  const newLabel = 'Synthetic shared update after company switch'
  await field(page, ui.display).fill(newLabel)
  const barrier = responseBarrier(state, (request) => request.method === 'PUT' && request.path === `/api/tools/${globalId}`)
  await button(page, ui.save).click()
  await bounded(barrier.entered, 'Scoped delayed write did not arrive')
  verify('scoped delayed metadata write disables Cancel', await button(page, ui.cancel).isDisabled())
  await selectCompany(page, state, tenantB)
  await detached(field(page, ui.display))
  await visible(button(page, ui.edit(globalTool.display_name)))
  const beforeLists = state.calls.filter((request) => request.method === 'GET' && request.params.tenant_id === tenantB).length
  barrier.release(); await bounded(barrier.completed, 'Detached-panel mutation did not complete')
  await visible(button(page, ui.edit(newLabel)))
  verify('accepted detached-panel write refetches the active other-company cache', state.calls.filter((request) => request.method === 'GET' && request.params.tenant_id === tenantB).length > beforeLists)
  verify('delayed write keeps original request company, not the new selection', mutations(state).at(-1).payload.tenant_id, tenantA)
  verify('stale callback never restores a switched-away panel', await field(page, ui.display).count(), 0)
  await selectCompany(page, state, tenantA)
  await visible(button(page, ui.edit(newLabel)))
}

try {
  let executable = process.env.CHROMIUM_EXECUTABLE
  if (!executable) { const { chromium } = await import('@playwright/test'); executable = chromium.executablePath() }
  await access(executable)
  profile = await mkdtemp(join(output, 'owned-profile-'))
  // Pre-proxy middleware OWNS every API path, including unknown ones. The resolved
  // proxy is also cleared: a handler defect cannot fall back to the real backend.
  // CSP removes remote HTTP(S) access without broad Fetch interception, whose
  // fulfill/continue commands race StrictMode's intentionally aborted requests.
  const csp = "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws://127.0.0.1:*; object-src 'none'; base-uri 'none'; form-action 'self'; frame-src 'none'"
  server = await createServer({ root, envDir: false, define: { 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') }, cacheDir: join(output, 'vite-cache'), plugins: [{
    name: 'tools-synthetic-firewall',
    configResolved(config) { config.server.proxy = {}; receipt.transport.proxyDisabled = Object.keys(config.server.proxy).length === 0 },
    configureServer(vite) { vite.middlewares.use((request, response, next) => {
      if (/^\/api(?:\/|\?|$)/u.test(request.url ?? '')) {
        const work = syntheticApi(activeScenarioState, request, response)
        inFlightApi.add(work); void work.finally(() => inFlightApi.delete(work))
      } else if (request.method !== 'GET' && request.method !== 'HEAD') {
        receipt.violations.push({ scenario: activeScenario, message: 'Blocked non-API write', url: request.url })
        response.writeHead(502); response.end('Synthetic firewall')
      } else next()
    }) },
  }], server: { host: '127.0.0.1', port: 0, headers: { 'Content-Security-Policy': csp }, watch: { ignored: (path) => path.startsWith(output) } } })
  verify('owned Vite API proxy is disabled', receipt.transport.proxyDisabled)
  await server.listen()
  browser = await omowright.connectPipe({ browserPath: executable, browserArgs: ['--headless', '--no-first-run', '--disable-background-networking', `--user-data-dir=${profile}`], storageRoot: profile, commandTimeoutMs: 10_000, dialogPolicy: { accept: false } })
  const toolsSource = await readFile(join(root, 'src/pages/tools.tsx'), 'utf8').catch((error) => { if (error.code === 'ENOENT') return ''; throw error })
  if (!toolsSource) {
    receipt.status = 'blocked-ui'
    receipt.blocked = 'src/pages/tools.tsx is not present. Confirm ui labels and Tools schema/type values once UI lands; no product scenarios are claimed.'
    await scenario({ name: 'placeholder-owned-engine-smoke' }, async (page, state) => {
      await capture(page, state)
      verify('placeholder makes no Tools writes', mutations(state).length, 0)
      verify('session was restored from synthetic auth', receipt.requests.some((request) => request.path === '/api/auth/me'))
      const wire = await page.evaluate(async (id) => {
        const request = async (path, method, payload) => { const response = await fetch(`/api/tools${path}`, { method, headers: { 'Content-Type': 'application/json' }, ...(payload ? { body: JSON.stringify(payload) } : {}) }); return { status: response.status, body: await response.json() } }
        const list = await request('', 'GET')
        const create = await request('', 'POST', { name: 'synthetic_wire_probe', display_name: 'Wire probe', type: 'mcp' })
        const update = await request(`/${id}`, 'PUT', { config: { api_key: 'synthetic-wire-secret' } })
        const masked = await request('', 'GET')
        const remove = await request(`/${id}`, 'DELETE')
        return { list, create, update, masked, remove }
      }, createdId)
      verify('wire list exposes stable fixtures', wire.list.body.map((tool) => tool.id), [builtinId, customId, globalId, foreignId])
      verify('wire POST contract', wire.create, { status: 200, body: { id: createdId, name: 'synthetic_wire_probe' } })
      verify('wire PUT contract', wire.update, { status: 200, body: { ok: true } })
      verify('wire config response stays masked', wire.masked.body.find((tool) => tool.id === createdId).config, { api_key: '********' })
      verify('wire DELETE contract', wire.remove, { status: 200, body: { ok: true } })
    })
  } else {
    await scenario({ name: 'oa-crud-masks-and-builtin-guard' }, exerciseCrud)
    await scenario({ name: 'oa-pending-write-barriers' }, exercisePendingWrites)
    await scenario({ name: 'pa-pending-write-company-switch-cache', role: 'platform_admin', scope: 'current' }, exerciseTenantSwitchPending)
    await scenario({ name: 'pa-tenant-scope-switching', role: 'platform_admin', scope: 'current' }, async (page, state) => {
      await visible(field(page, ui.company))
      verify('PA must choose a concrete company before creating', await button(page, ui.add).isDisabled())
      verify('PA current scope cannot replace configuration', await button(page, ui.replace(longLabel)).isDisabled())
      await assertDeniedControl(button(page, ui.replace(longLabel)))
      for (const tool of [builtin, custom, globalTool, foreignTool]) {
        verify('PA may edit details of any record', await button(page, ui.edit(tool.display_name)).isDisabled(), false)
        if (tool.type !== 'builtin') verify('PA may delete nonbuiltin records regardless of owner', await button(page, ui.remove(tool.display_name)).isDisabled(), false)
      }
      verify('PA builtin delete stays hidden', await button(page, ui.remove(builtin.display_name)).count(), 0)
      for (const scope of [tenantA, tenantB, tenantA]) {
        await selectCompany(page, state, scope)
        await visible(button(page, ui.edit(scope === tenantA ? longLabel : 'Synthetic Beta connector')))
      }
      verify('both selected tenant scopes reached wire', [...new Set(state.calls.map((call) => call.params.tenant_id).filter(Boolean))].sort(), [tenantA, tenantB].sort())
      await assertOwnership(page, globalTool, 'Global')
      await assertOwnership(page, foreignTool, 'Company')
      for (const tool of [builtin, custom, globalTool, foreignTool]) verify('PA may configure any record with a concrete company', await button(page, ui.replace(tool.display_name)).isDisabled(), false)
      await button(page, ui.edit(longLabel)).click()
      await field(page, ui.display).fill('Synthetic unsaved stale draft')
      await selectCompany(page, state, tenantB)
      await detached(field(page, ui.display))
      await visible(button(page, ui.edit('Synthetic Beta connector')))
      verify('switching company discards an unsaved editor without writes', mutations(state).length, 0)
      await selectCompany(page, state, tenantA)
      await visible(button(page, ui.edit(longLabel)))
      await button(page, ui.edit(longLabel)).click()
      await field(page, ui.display).fill('Synthetic scoped edit'); await button(page, ui.save).click()
      await visible(button(page, ui.edit('Synthetic scoped edit')))
      verify('PA mutation carries selected tenant', mutations(state).at(-1).payload.tenant_id, tenantA)
    })
    for (const dimensions of matrix.filter(({ width }) => width === 390 || width === 1440)) {
      await scenario({ ...dimensions, name: `pa-selector-${dimensions.width}-${dimensions.theme}`, role: 'platform_admin', scope: 'current' }, async (page, state) => {
        await visible(field(page, ui.company)); await visible(button(page, ui.edit(longLabel)))
        verify('PA no-selection catalog still requires a concrete company for create', await button(page, ui.add).isDisabled())
        for (const scope of ['current', tenantA, tenantB]) {
          if (scope !== 'current') await selectCompany(page, state, scope)
          activeScenario = `${state.name}-${scope === 'current' ? 'no-selection' : scope === tenantA ? 'alpha' : 'beta'}`
          await field(page, ui.company).focus()
          await field(page, ui.company).evaluate((element) => element.scrollIntoView({ block: 'center', behavior: 'instant' }))
          await assertInViewport(field(page, ui.company), 'PA company selector is visible in the capture')
          verify('PA company selector retains selected context', await field(page, ui.company).inputValue(), scope === 'current' ? '' : scope)
          await capture(page, state)
        }
      })
    }
    for (const dimensions of matrix) {
      await scenario({ ...dimensions, name: `tools-${dimensions.width}-${dimensions.theme}-list` }, captureMatrix)
      await scenario({ ...dimensions, name: `tools-${dimensions.width}-${dimensions.theme}-loading`, loading: true }, async (page, state) => {
        await bounded(state.loadingBarrier.entered, 'Catalog request never reached the controlled loading barrier')
        await visible(page.locator('[role="status"]').filter({ hasText: exactText('Loading tools…') }))
        verify('loading state is held by an unreleased response barrier', state.loadingBarrier.released, false)
        verify('loading capture does not misrepresent signed-out/auth failure as success', await page.locator('main h1, main h2, main h3').filter({ hasText: /^Tools$/u }).count(), 1)
        await capture(page, state)
        state.loadingBarrier.release()
        await visible(button(page, ui.edit(longLabel)))
      })
      for (const mode of ['empty', 'error']) await scenario({ ...dimensions, name: `tools-${dimensions.width}-${dimensions.theme}-${mode}`, ...(mode === 'empty' ? { catalog: { [tenantA]: [], [tenantB]: [] } } : { status: 503 }) }, async (page, state) => {
        await visible(mode === 'empty' ? page.getByText(ui.empty, { exact: true }) : button(page, ui.retry))
        await capture(page, state)
        await page.evaluate(() => { const main = document.querySelector('main'); if (main) main.scrollTop = main.scrollHeight })
        activeScenario += '-scrolled-end-keyboard'
        const control = button(page, mode === 'empty' ? ui.add : ui.retry)
        await control.focus(); await assertFocus(page, control); await capture(page, state)
        if (mode === 'error') { state.status = 200; await button(page, ui.retry).click(); await visible(button(page, ui.edit(longLabel))) }
      })
    }
    verify('all created response barriers were released', receipt.cleanup.barriersReleased, receipt.cleanup.barriersCreated)
    verify('no API interception routes were created', receipt.cleanup.apiRoutesCreated, 0)
    verify('owned tab cleanup is complete', receipt.cleanup.tabsClosed, receipt.cleanup.tabsCreated)
    receipt.status = receipt.findings.length ? 'failed' : 'passed'
    if (receipt.findings.length) process.exitCode = 1
  }
} catch (error) {
  receipt.status = 'failed'
  receipt.error = String(error)
  process.exitCode = 1
} finally {
  for (const [key, cleanup] of [
    ['browserClosed', async () => { if (browser) await browser.close() }],
    ['viteClosed', async () => { if (server) await server.close() }],
    ['profileRemoved', async () => { if (profile) await rm(profile, { recursive: true, force: true }) }],
  ]) { try { await cleanup(); receipt.cleanup[key] = true } catch (error) { receipt.cleanup[key] = String(error); receipt.status = 'failed'; process.exitCode = 1 } }
  receipt.finishedAt = new Date().toISOString()
  receipt.actualMatrix = { scenarios: receipt.scenarios.length, captures: receipt.screenshots.length, assertions: receipt.assertions.length }
  await writeFile(join(output, 'matrix-manifest.json'), JSON.stringify({ status: receipt.status, planned: matrix, captures: receipt.screenshots }, null, 2))
  await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  console.log(JSON.stringify({ status: receipt.status, output, assertions: receipt.assertions.length, screenshots: receipt.screenshots.length, cleanup: receipt.cleanup, blocked: receipt.blocked }))
}
