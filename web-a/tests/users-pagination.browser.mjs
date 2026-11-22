import assert from 'node:assert/strict'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { expect } from '@playwright/test'
import { createServer } from 'vite'
import { uncappedOverview } from './capacity-fixtures.ts'
import { launchRosterBrowser } from './roster-export-owned-browser.mjs'
import { exerciseRosterExports, installRosterProbe } from './roster-export-browser-cases.mjs'

const output = process.env.USERS_PAGINATION_ARTIFACTS ?? join(process.env.TMPDIR, 'users-pagination-evidence')
await mkdir(output, { recursive: true })
let downloadState
const cacheDir = join(output, 'vite-cache')
const server = await createServer({ configLoader: 'runner', cacheDir, server: { host: '127.0.0.1', port: 0 }, plugins: [{
  name: 'owned-native-roster-download',
  configureServer(vite) {
    vite.middlewares.use((request, response, next) => {
      const url = new URL(request.url, 'http://127.0.0.1')
      if (url.pathname !== '/api/users/export/download') return next()
      assert.ok(downloadState)
      assert.deepEqual([...url.searchParams.keys()], ['ticket'])
      assert.equal(request.headers.authorization, undefined)
      assert.equal(request.headers.referer, undefined)
      const ticket = url.searchParams.get('ticket')
      assert.equal(downloadState.tickets.delete(ticket), true)
      downloadState.redemptions.push(ticket)
      response.writeHead(200, { 'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': 'attachment; filename="company-users.csv"', 'Cache-Control': 'no-store' })
      response.end(downloadState.downloadCsv)
    })
  },
}] })
let browser
let ownedBrowser
const evidence = []
const scrollOwners = []
const alpha = 'c1111111-1111-4111-8111-111111111111'
const beta = 'c2222222-2222-4222-8222-222222222222'
const gamma = 'c4444444-4444-4444-8444-444444444444'
const missing = 'c5555555-5555-4555-8555-555555555555'
const row = (id, tenant = alpha, extra = {}) => ({
  id, email: `${id}@example.test`, display_name: id,
  role: 'member', is_active: false, is_genesis: false, tenant_id: tenant,
  agents_count: 2, created_at: '2026-10-20T00:00:00Z', source: 'local', ...extra,
})
const admin = {
  ...row('Operator'), id: 'a1111111-1111-4111-8111-111111111111',
  tenant_id: 'a3333333-3333-4333-8333-333333333333',
  identity_id: 'a2222222-2222-4222-8222-222222222222', avatar_url: null,
  role: 'platform_admin', is_platform_admin: true, is_genesis: true,
  email_verified: true, must_change_password: false, is_active: true,
}
// Gamma and the operator's own company are never on the browse pages: only search or a by-id lookup reaches them.
const directory = { [alpha]: ['alpha', 'Alpha Labs'], [beta]: ['beta', 'Beta Works'], [gamma]: ['gamma', 'Gamma Archive'], [admin.tenant_id]: ['operator', 'Operator Org'] }
const company = (id) => ({ id, slug: directory[id][0], name: directory[id][1], is_active: true, can_disable: true, tier: 'pro' })
const slug = (tenant) => directory[tenant]?.[0] ?? tenant
// StrictMode's dev double mount aborts and repeats signal-aware reads, so compare distinct requests.
const distinct = (requests) => [...new Set(requests.map((request) => JSON.stringify(request)))].map((request) => JSON.parse(request))
const cursor = 'opaque/+?=next'
const department = { id: 'operations', tenant_id: alpha, name: 'Operations', description: '', created_at: '2026-10-20T00:00:00Z' }
const first = { items: [row('First member'), row('Second admin', alpha, { role: 'org_admin' })], next_cursor: cursor }

async function scenario(name, exercise, options = {}) {
  if (process.env.USERS_PAGINATION_SCENARIO && !name.includes(process.env.USERS_PAGINATION_SCENARIO)) return
  const context = await browser.newContext({ viewport: { width: 1365, height: 1000 } })
  context.setDefaultTimeout(10_000)
  const state = {
    role: options.role ?? 'platform_admin', calls: [], assignmentCalls: [], assignments: [], patches: [], companyCalls: 0, gates: [], exports: [], downloads: [], exportReplies: 0,
    optionPages: [[alpha, beta]], optionRequests: [], tenantLookups: [], missingTenants: new Set(),
    exportAnswer: async () => ({ ticket: `issued-${state.exports.length}`, expires_in: 60 }),
    downloadCsv: 'id,email\r\nserver-only,off-page@example.test\r\n', tickets: new Set(), redemptions: [],
    answer: async ({ tenant, search, cursor: next }) => ({ items: [row(search || (next ? 'Last member' : `${slug(tenant)} member`), tenant)], next_cursor: null }),
  }
  const errors = []
  downloadState = state
  try {
    await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-token'))
    await context.addInitScript(installRosterProbe)
    const page = await context.newPage()
    page.on('download', (download) => state.downloads.push(download))
    page.on('pageerror', (error) => errors.push(error.message))
    await page.route('**/api/**', async (route) => {
      const request = route.request()
      const url = new URL(request.url())
      const path = url.pathname
      let body
      let status = 200
      const currentAdmin = { ...admin, role: state.role, is_platform_admin: state.role === 'platform_admin' }
      if (path === '/api/auth/me') body = currentAdmin
      else if (path === '/api/auth/login') body = { access_token: 'synthetic-token', token_type: 'bearer', user: currentAdmin }
      else if (path === '/api/admin/companies/options') {
        const q = url.searchParams.get('q')
        const at = url.searchParams.get('cursor')
        assert.equal(url.searchParams.get('limit'), '50')
        state.companyCalls++
        state.optionRequests.push({ q, cursor: at })
        if (q) body = { items: Object.keys(directory).filter((id) => directory[id][1].toLowerCase().includes(q.toLowerCase())).map(company), next_cursor: null }
        else {
          const index = at ? Number(at.split('=').at(-1)) : 0
          body = { items: state.optionPages[index].map(company), next_cursor: index + 1 < state.optionPages.length ? `opts/+=${index + 1}` : null }
        }
      }
      else if (path === '/api/admin/platform-admins') body = [row('Hidden supervisor', null, { role: 'platform_admin' })]
      else if (path === '/api/capacity/overview') body = { ...uncappedOverview, tenant_id: url.searchParams.get('tenant_id') ?? admin.tenant_id }
      else if (path === '/api/departments') body = { departments: options.departments ?? [] }
      else if (path === '/api/departments/assignments') {
        const tenant = url.searchParams.get('tenant_id')
        const userIds = url.searchParams.getAll('user_id')
        assert.ok(userIds.length > 0 && userIds.length <= 100)
        state.assignmentCalls.push({ tenant, userIds })
        body = state.assignments.filter((row) => row.tenant_id === tenant && userIds.includes(row.user_id))
      }
      else if (path === '/api/notifications/unread-count') body = { unread_count: 0 }
      else if (path === '/api/admin/search-analytics/summary') body = { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' }
      else if (path === '/api/admin/search-analytics/orgs') body = []
      else if (path === '/api/tenants/me') body = { ...company(admin.tenant_id), is_default_end_user_org: false }
      else if (path.startsWith('/api/tenants/')) {
        const id = decodeURIComponent(path.slice('/api/tenants/'.length))
        state.tenantLookups.push(id)
        if (directory[id] && !state.missingTenants.has(id)) body = company(id)
        else { status = 404; body = { detail: 'Tenant not found' } }
      }
      else if (path === '/api/users/page') {
        const input = { tenant: url.searchParams.get('tenant_id'), search: url.searchParams.get('search'), cursor: url.searchParams.get('cursor') }
        assert.equal(url.searchParams.get('limit'), '50')
        state.calls.push(input)
        const result = await state.answer(input)
        status = result.status ?? 200
        body = result.body ?? result
      } else if (path === '/api/users/export') {
        assert.equal(request.method(), 'POST')
        const input = { tenant: url.searchParams.get('tenant_id'), search: url.searchParams.get('search') }
        assert.deepEqual([...url.searchParams.keys()].sort(), ['search', 'tenant_id'])
        state.exports.push(input)
        const result = await state.exportAnswer(input)
        status = result.status ?? 200
        body = result.body ?? result
        if (status === 200 && typeof body.ticket === 'string') state.tickets.add(body.ticket)
      } else if (path.endsWith('/active') && request.method() === 'PATCH') {
        state.patches.push({ path, body: request.postDataJSON() })
        body = row(decodeURIComponent(path.split('/').at(-2)), alpha, { is_active: true })
      } else { errors.push(`Unexpected API ${request.method()} ${path}`); status = 501; body = { detail: 'Unexpected synthetic route' } }
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
      if (path === '/api/users/export') state.exportReplies++
    })
    await exercise(page, state)
    assert.deepEqual(errors, [])
    evidence.push(name)
    console.log(`PASS ${name}`)
  } finally {
    for (const gate of state.gates) gate.resolve()
    if (browser.isConnected()) await context.close()
    downloadState = undefined
  }
}

const openUser = (page, name) => page.getByRole('link', { name: `Open ${name}`, exact: true })
const openUsers = async (page, query = `company=${alpha}`) => {
  await page.goto(`${server.resolvedUrls.local[0]}users?${query}`)
  await expect(page.getByRole('heading', { name: 'Users', exact: true })).toBeVisible()
}
const gated = (state) => {
  const gate = Promise.withResolvers()
  state.gates.push(gate)
  return gate
}

try {
  await server.listen()
  ownedBrowser = await launchRosterBrowser()
  browser = ownedBrowser.browser
  await scenario('server paging preserves ordering, separate platform admins and departments, then exhausts', async (page, state) => {
    // Given
    state.answer = async ({ cursor: next }) => next ? { items: [row('Last member')], next_cursor: null } : first
    state.assignments = ['First member', 'Last member'].map((user_id) => ({ user_id, tenant_id: alpha, department_id: department.id }))
    const operations = (name) => page.getByRole('group', { name: `Departments for ${name}`, exact: true })
      .getByRole('checkbox', { name: 'Operations', exact: true })
    await openUsers(page)
    await expect(openUser(page, 'First member')).toBeVisible()
    await expect(openUser(page, 'Hidden supervisor')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Departments', exact: true })).toBeVisible()
    await expect(operations('First member')).toBeChecked()
    assert.deepEqual(state.assignmentCalls, [{ tenant: alpha, userIds: ['First member', 'Second admin'] }])
    const companyLinks = page.getByRole('link', { name: /^Open (First member|Second admin)$/ })
    assert.deepEqual(await companyLinks.evaluateAll((links) => links.map((link) => link.getAttribute('aria-label'))), ['Open First member', 'Open Second admin'])
    await page.getByRole('button', { name: 'Load more users' }).focus()
    await ownedBrowser.capture(page, join(output, 'users-pagination-keyboard-focus.png'))
    // When
    await page.getByRole('button', { name: 'Load more users' }).click()
    // Then
    await expect(openUser(page, 'Last member')).toBeVisible()
    await expect(operations('Last member')).toBeChecked()
    await expect(operations('First member')).toBeChecked()
    assert.deepEqual(state.assignmentCalls, [
      { tenant: alpha, userIds: ['First member', 'Second admin'] },
      { tenant: alpha, userIds: ['Last member'] },
    ])
    await expect(page.getByRole('button', { name: 'Load more users' })).toHaveCount(0)
    await expect(page.getByText('All matching users loaded.')).toBeVisible()
    assert.deepEqual(state.calls, [{ tenant: alpha, search: '', cursor: null }, { tenant: alpha, search: '', cursor }])
    for (const width of [390, 1440, 1920]) for (const colorScheme of ['light', 'dark']) {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 })
      await ownedBrowser.emulate(page, width === 390 ? 'iphone-14' : { width, height: 1000, deviceScaleFactor: 1, mobile: false, hasTouch: false })
      await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' })
      await page.evaluate(() => document.fonts.ready)
      scrollOwners.push({ width, colorScheme, ...await page.evaluate(() => ({
        width: innerWidth, dpr: devicePixelRatio,
        owners: [...document.querySelectorAll('*')].filter((element) => /auto|scroll/.test(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight).map((element) => element.tagName.toLowerCase()),
      })) })
      for (const position of ['top', 'end']) {
        await page.locator('main').evaluate((main, end) => { main.scrollTop = end ? main.scrollHeight : 0 }, position === 'end')
        await ownedBrowser.capture(page, join(output, `users-pagination-${width}-${colorScheme}-${position}.png`))
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true)
    }
  }, { departments: [department] })

  await scenario('search reaches off-page users, preserves literal input, clears old rows and supports empty results', async (page, state) => {
    // Given
    const gate = gated(state)
    const started = Promise.withResolvers()
    state.answer = async ({ search }) => {
      if (!search) return first
      if (search === 'Hidden %_+') { started.resolve(); await gate.promise; return { items: [row('Off-page match')], next_cursor: null } }
      return { items: [], next_cursor: null }
    }
    await openUsers(page)
    await expect(openUser(page, 'First member')).toBeVisible()
    // When
    await page.getByLabel('Search', { exact: true }).fill('Hidden %_+')
    await started.promise
    // Then
    await expect(openUser(page, 'First member')).toHaveCount(0)
    await expect(page.getByText('Loading users…', { exact: true })).toBeVisible()
    gate.resolve()
    await expect(openUser(page, 'Off-page match')).toBeVisible()
    assert.deepEqual(state.calls.at(-1), { tenant: alpha, search: 'Hidden %_+', cursor: null })
    await page.getByLabel('Search', { exact: true }).fill('missing')
    await expect(page.getByText('No users match “missing”.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Load more users' })).toHaveCount(0)
  })

  for (const nextPage of [false, true]) await scenario(`${nextPage ? 'continuation' : 'first-page'} failure has scoped retry without discarding loaded rows`, async (page, state) => {
    // Given
    let rejected = true
    state.answer = async ({ cursor: next }) => Boolean(next) === nextPage && rejected
      ? { status: 503, body: { detail: 'Users temporarily unavailable' } }
      : next ? { items: [row('Recovered member')], next_cursor: null } : first
    await openUsers(page)
    if (nextPage) { await expect(openUser(page, 'First member')).toBeVisible(); await page.getByRole('button', { name: 'Load more users' }).click() }
    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toContainText('Users temporarily unavailable')
    rejected = false
    // When
    await page.getByRole('button', { name: 'Retry users' }).click()
    // Then
    await expect(openUser(page, nextPage ? 'Recovered member' : 'First member')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Retry users' })).toHaveCount(0)
    if (nextPage) await expect(openUser(page, 'First member')).toBeVisible()
    assert.equal(state.calls.at(-1).cursor, nextPage ? cursor : null)
  })

  for (const transition of ['company', 'search']) for (const status of [200, 403]) await scenario(`late cursor ${status} cannot leak across ${transition} changes or restore a previous visit`, async (page, state) => {
    // Given
    const gate = gated(state)
    const started = Promise.withResolvers()
    state.answer = async ({ tenant, search, cursor: next }) => {
      if (next) { started.resolve(); await gate.promise; return { status, body: status === 200 ? { items: [row('Retired member')], next_cursor: null } : { detail: 'Retired error' } } }
      return tenant === alpha && !search ? first : { items: [row('Current member', tenant)], next_cursor: null }
    }
    await openUsers(page)
    await expect(openUser(page, 'First member')).toBeVisible()
    await page.getByRole('button', { name: 'Load more users' }).click()
    await started.promise
    // When
    if (transition === 'company') await page.getByRole('combobox').selectOption(beta)
    else await page.getByLabel('Search', { exact: true }).fill('current')
    // Then
    await expect(openUser(page, 'Current member')).toBeVisible()
    gate.resolve()
    await expect(openUser(page, 'Retired member')).toHaveCount(0)
    await expect(page.getByText('Retired error', { exact: true })).toHaveCount(0)
    if (transition === 'company') await page.getByRole('combobox').selectOption(alpha)
    else await page.getByLabel('Search', { exact: true }).fill('')
    await expect(openUser(page, 'First member')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Load more users' })).toBeEnabled()
    assert.deepEqual(state.calls.at(-1), { tenant: alpha, search: '', cursor: null })
  })

  await scenario('same-token login and role replacement retire pages and use the OA own company despite URL scope', async (page, state) => {
    // Given
    const gate = gated(state)
    const started = Promise.withResolvers()
    state.answer = async ({ cursor: next }) => {
      if (next) { started.resolve(); await gate.promise; return { items: [row('Retired session')], next_cursor: null } }
      return state.role === 'platform_admin' ? first : { items: [row('Replacement member')], next_cursor: null }
    }
    await openUsers(page, `company=${beta}`)
    await expect(openUser(page, 'First member')).toBeVisible()
    await page.getByRole('button', { name: 'Load more users' }).click()
    await started.promise
    // When
    await page.getByRole('button', { name: 'Sign out', exact: true }).click()
    state.role = 'org_admin'
    await page.getByLabel('Email', { exact: true }).fill('operator@example.test')
    await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    // Then
    await expect(openUser(page, 'Replacement member')).toBeVisible()
    gate.resolve()
    await expect(openUser(page, 'Retired session')).toHaveCount(0)
    await expect(openUser(page, 'Hidden supervisor')).toHaveCount(0)
    await expect(page.getByRole('combobox')).toHaveCount(0)
    assert.deepEqual(state.calls.at(-1), { tenant: admin.tenant_id, search: '', cursor: null })
  })

  for (const selection of ['foreign', missing]) await scenario(`unknown company ${selection === missing ? 'id' : 'value'} keeps the deep link and never starts an unscoped or foreign users request`, async (page, state) => {
    // Given / When
    await openUsers(page, `company=${selection}`)
    // Then
    const picker = page.getByRole('combobox')
    await expect(picker.locator('option:checked')).toHaveText('Unknown company')
    await expect(openUser(page, 'Hidden supervisor')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Export company CSV', exact: true })).toBeDisabled()
    assert.deepEqual(state.calls, [])
    assert.deepEqual(distinct(state.tenantLookups), selection === missing ? [missing] : [])
    assert.equal(new URL(page.url()).searchParams.get('company'), selection)
    // When
    await picker.selectOption(alpha)
    // Then
    await expect(openUser(page, 'alpha member')).toBeVisible()
    assert.deepEqual(state.calls, [{ tenant: alpha, search: '', cursor: null }])
  })

  await scenario('platform admin defaults to the own company resolved by id even when it is off the loaded options', async (page, state) => {
    // Given / When
    await openUsers(page, '')
    // Then
    await expect(openUser(page, 'operator member')).toBeVisible()
    await expect(page.getByRole('combobox').locator('option:checked')).toHaveText('Operator Org')
    assert.equal(new URL(page.url()).searchParams.get('company'), admin.tenant_id)
    assert.deepEqual(state.calls, [{ tenant: admin.tenant_id, search: '', cursor: null }])
    assert.equal(state.tenantLookups.includes(admin.tenant_id), true)
    assert.equal(state.optionRequests.every((request) => request.q === null && request.cursor === null), true)
  })

  await scenario('a failed own-company lookup falls back to the first loaded company', async (page, state) => {
    // Given
    state.missingTenants.add(admin.tenant_id)
    // When
    await openUsers(page, '')
    // Then
    await expect(openUser(page, 'alpha member')).toBeVisible()
    await expect(page.getByRole('combobox').locator('option:checked')).toHaveText('Alpha Labs')
    assert.equal(new URL(page.url()).searchParams.get('company'), alpha)
    assert.deepEqual(state.calls, [{ tenant: alpha, search: '', cursor: null }])
  })

  await scenario('off-page deep link resolves by id and stays pinned once while load more and search reach other companies', async (page, state) => {
    // Given
    state.optionPages = [[alpha], [beta]]
    const picker = page.getByRole('combobox')
    const loadMore = page.getByRole('button', { name: 'Load more companies', exact: true })
    // When
    await openUsers(page, `company=${gamma}`)
    // Then
    await expect(openUser(page, 'gamma member')).toBeVisible()
    await expect(picker.locator('option')).toHaveText(['Gamma Archive', 'Alpha Labs'])
    await expect(picker.locator('option:checked')).toHaveText('Gamma Archive')
    assert.deepEqual(state.calls, [{ tenant: gamma, search: '', cursor: null }])
    assert.deepEqual(distinct(state.tenantLookups), [gamma])
    await loadMore.click()
    await expect(picker.locator('option')).toHaveText(['Gamma Archive', 'Alpha Labs', 'Beta Works'])
    await expect(loadMore).toHaveCount(0)
    await expect(picker).toBeFocused()
    assert.deepEqual(distinct(state.optionRequests), [{ q: null, cursor: null }, { q: null, cursor: 'opts/+=1' }])
    await page.getByLabel('Search companies by name', { exact: true }).fill('r')
    await expect(picker.locator('option')).toHaveText(['Beta Works', 'Gamma Archive', 'Operator Org'])
    await expect(picker.locator('option:checked')).toHaveText('Gamma Archive')
    await expect(loadMore).toHaveCount(0)
    assert.deepEqual(state.optionRequests.at(-1), { q: 'r', cursor: null })
    await ownedBrowser.capture(page, join(output, 'users-company-picker-search.png'))
    // When
    await picker.selectOption(beta)
    // Then
    await expect(openUser(page, 'beta member')).toBeVisible()
    assert.deepEqual(state.calls.at(-1), { tenant: beta, search: '', cursor: null })
    await expect(picker.locator('option')).toHaveText(['Beta Works', 'Gamma Archive', 'Operator Org'])
  })

  await scenario('activation resets accumulated pages and invalidates company counts', async (page, state) => {
    // Given
    state.answer = async ({ cursor: next }) => next ? { items: [row('Last member')], next_cursor: null }
      : { items: [row('First member', alpha, { is_active: state.patches.length > 0 })], next_cursor: cursor }
    await openUsers(page)
    await expect(openUser(page, 'First member')).toBeVisible()
    await page.getByRole('button', { name: 'Load more users' }).click()
    await expect(openUser(page, 'Last member')).toBeVisible()
    const card = page.locator('[data-slot="card"]').filter({ has: openUser(page, 'First member') })
    // When
    await card.getByRole('button', { name: 'Activate First member', exact: true }).click()
    // Then
    await expect(card.getByRole('button', { name: 'Deactivate First member', exact: true })).toBeVisible()
    await expect(openUser(page, 'Last member')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Load more users' })).toBeEnabled()
    assert.deepEqual(state.calls.at(-1), { tenant: alpha, search: '', cursor: null })
    assert.deepEqual(state.patches, [{ path: '/api/users/First%20member/active', body: { is_active: true } }])
    await expect.poll(() => state.companyCalls).toBeGreaterThan(1)
  })
  await exerciseRosterExports(scenario, { openUsers, openUser, gated, admin, output, ownedBrowser, companies: { alpha, beta } })
  await writeFile(join(output, 'scroll-owners.json'), JSON.stringify(scrollOwners, null, 2))
  await writeFile(join(output, 'results.json'), JSON.stringify(evidence, null, 2))
} finally {
  await ownedBrowser?.close()
  await server.close()
  await rm(cacheDir, { recursive: true, force: true })
  await writeFile(join(output, 'teardown.json'), JSON.stringify({ browserClosed: true, serverClosed: true, cacheRemoved: true, ...ownedBrowser?.resources }, null, 2))
}
