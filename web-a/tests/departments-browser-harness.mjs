import assert from 'node:assert/strict'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { devices, expect } from '@playwright/test'
import { createServer } from 'vite'
import { launchDepartmentBrowser } from './departments-owned-browser.mjs'

export const tenant = '86b8142b-31e6-44ab-a35e-e10998037030'
export const otherTenant = '64c0b6ca-0312-42aa-996d-ff606043337a'
export const departmentId = 'department/a +?'
export const mergeSourceId = '10000000-0000-4000-8000-000000000001'
export const mergeDestinationId = '10000000-0000-4000-8000-000000000002'
export const admission = {
  id: '381bf275-fd20-4b0f-a79d-1f2c4519d258', status: 'withdrawn',
  source_tenant_id: otherTenant, source_tenant: { id: otherTenant, name: 'Source company' },
  target_tenant: { id: tenant, name: 'Alpha Labs' }, method: 'invitation',
  created_at: '2026-10-20T09:00:00Z', decided_at: '2026-10-20T10:00:00Z',
  decision_reason: null, decided_by_user_id: null,
  applicant: { id: '9693e39c-ec62-4003-9701-7b5b530c9662', email: 'applicant@example.test' },
}
const row = (id, tenantId = tenant) => ({ id, email: `${id}@example.test`, display_name: id, role: 'member', is_active: true, is_genesis: false, tenant_id: tenantId, agents_count: 0, created_at: '2026-10-20T00:00:00Z', source: 'local' })
const admin = { ...row('operator'), id: '11111111-1111-4111-8111-111111111111', identity_id: '11111111-1111-4111-8111-111111111111', avatar_url: null, title: null, primary_mobile: null, registration_source: null, role: 'platform_admin', is_platform_admin: true, is_genesis: true, email_verified: true, must_change_password: false }
export const directory = (name = 'Operations', tenantId = tenant) => ({
  departments: [{ id: departmentId, tenant_id: tenantId, name, created_at: '2026-10-20T00:00:00Z' }, { id: 'engineering', tenant_id: tenantId, name: 'Engineering', created_at: '2026-10-20T00:00:00Z' }],
  assignments: [{ user_id: 'Member', tenant_id: tenantId, department_id: departmentId }, { user_id: 'Member', tenant_id: tenantId, department_id: 'engineering' }],
})

export async function createHarness() {
  const output = process.env.DEPARTMENT_ARTIFACTS ?? await mkdtemp(join(tmpdir(), 'department-evidence-'))
  await mkdir(output, { recursive: true })
  const cacheDir = join(output, 'vite-cache')
  const server = await createServer({ cacheDir, server: { host: '127.0.0.1', port: 0, watch: { ignored: (path) => path.startsWith(output) } } })
  const receipt = { engine: 'omowright.connectPipe; Playwright CDP assertions', scenarios: [], screenshots: [], requests: [], cleanup: { browsersCreated: 0, browsersClosed: 0, profilesRemoved: 0, contextsCreated: 0, contextsClosed: 0, viteClosed: false, cacheRemoved: false, syntheticAPIsOnly: true } }
  await server.listen()
  const base = server.resolvedUrls.local[0]

  async function scenario(options, exercise) {
    const owned = await launchDepartmentBrowser()
    const browser = owned.browser
    receipt.cleanup.browsersCreated++
    const state = { role: options.role ?? 'platform_admin', directories: { [tenant]: directory(), [otherTenant]: directory('Beta operations', otherTenant) }, calls: [], deleteStatus: 204, gate: null, exchanges: [], previewStatus: 200, preview: { membership_count: 1, agent_grant_count: 3 }, previewGate: null, previews: [], admission: { ...admission }, mergeStatus: 200, mergePreviewStatus: 200, mergeGate: null, mergePreviewGate: null, mergePreviews: [], merges: [] }
    const errors = []
    const context = await browser.newContext({ ...(options.mobile ? devices['iPhone 14'] : { viewport: { width: options.width ?? 1440, height: 1000 } }), colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce' })
    receipt.cleanup.contextsCreated++
    const page = await context.newPage()
    page.setDefaultTimeout(10_000)
    try {
      await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-department-token'))
      page.on('pageerror', (error) => errors.push(error.message))
      await page.route('**/api/**', async (route) => {
        const request = route.request()
        const url = new URL(request.url())
        const path = url.pathname
        const selected = url.searchParams.get('tenant_id') ?? tenant
        const call = { method: request.method(), path, params: Object.fromEntries(url.searchParams) }
        state.calls.push(call)
        receipt.requests.push({ scenario: options.name, ...call })
        const user = { ...admin, role: state.role, is_platform_admin: state.role === 'platform_admin' }
        let body
        let status = 200
        let exchange
        if (path === '/api/auth/me') body = user
        else if (path === '/api/tenants/me') body = { id: tenant, is_active: true, is_default_end_user_org: false }
        else if (path === '/api/auth/login') body = { access_token: 'synthetic-department-token', token_type: 'bearer', user }
        else if (path === '/api/admin/companies') body = [{ id: tenant, name: 'Alpha Labs', slug: 'alpha', is_active: true }, { id: otherTenant, name: 'Beta Works', slug: 'beta', is_active: true }]
        else if (path === '/api/admin/platform-admins') body = []
        else if (path === '/api/users/page') body = { items: [row('Member', selected)], next_cursor: null }
        else if (path === '/api/departments') body = state.directories[selected]
        else if (path.endsWith('/merge-preview') || path.endsWith('/merge')) {
          const apply = request.method() === 'POST'
          const payload = apply ? request.postDataJSON() : null
          const destinationId = payload?.destination_id ?? url.searchParams.get('destination_id')
          const current = state.directories[selected]
          const sourceId = decodeURIComponent(path.split('/')[3])
          const source = current.departments.find((item) => item.id === sourceId)
          const destination = current.departments.find((item) => item.id === destinationId)
          status = apply ? state.mergeStatus : state.mergePreviewStatus
          body = status === 200 ? {
            source, destination, source_membership_count: 2, destination_membership_count: 2, overlapping_membership_count: 1,
            source_agent_grant_count: 3, destination_agent_grant_count: 4, overlapping_agent_grant_count: 2,
            ...state.mergeCounts,
          } : { detail: apply ? 'Synthetic merge denied' : 'Synthetic merge preview unavailable' }
          exchange = { gate: apply ? state.mergeGate : state.mergePreviewGate, delivered: Promise.withResolvers(), payload, ...call }
          ;(apply ? state.merges : state.mergePreviews).push(exchange)
          if (exchange.gate) await exchange.gate.promise
          if (apply && status === 200) {
            current.departments = current.departments.filter((item) => item.id !== sourceId)
            const destinations = new Set(current.assignments.filter((item) => item.department_id === destinationId).map((item) => item.user_id))
            current.assignments = current.assignments.flatMap((item) => item.department_id !== sourceId ? [item] : destinations.has(item.user_id) ? [] : [{ ...item, department_id: destinationId }])
          }
        }
        else if (path === `/api/departments/${encodeURIComponent(departmentId)}/deletion-preview` && request.method() === 'GET') {
          status = state.previewStatus
          body = status === 200 ? state.preview : { detail: 'Synthetic preview unavailable' }
          exchange = { gate: state.previewGate, delivered: Promise.withResolvers() }
          state.previews.push(exchange)
          if (exchange.gate) await exchange.gate.promise
        }
        else if (path === `/api/departments/${encodeURIComponent(departmentId)}` && request.method() === 'DELETE') {
          status = state.deleteStatus
          const current = state.directories[selected]
          exchange = { gate: state.gate, delivered: Promise.withResolvers() }
          state.exchanges.push(exchange)
          if (exchange.gate) await exchange.gate.promise
          if (status === 204) {
            current.departments = current.departments.filter((item) => item.id !== departmentId)
            current.assignments = current.assignments.filter((item) => item.department_id !== departmentId)
          } else body = { detail: 'Synthetic department deletion denied' }
        } else if (path === '/api/company-admissions/review') body = url.searchParams.get('status') === state.admission.status ? [state.admission] : []
        else if (path === `/api/company-admissions/${admission.id}`) body = state.admission
        else if (path === '/api/notifications/unread-count') body = { unread_count: 0 }
        else if (path === '/api/admin/search-analytics/summary') body = { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' }
        else if (path === '/api/admin/search-analytics/orgs') body = []
        else { errors.push(`Unexpected synthetic API ${request.method()} ${path}`); await route.abort(); return }
        try {
          await route.fulfill(status === 204 ? { status } : { status, contentType: 'application/json', body: JSON.stringify(body) })
        } finally { exchange?.delivered.resolve() }
      })
      await exercise(page, state, base)
      assert.deepEqual(errors, [])
      receipt.scenarios.push(options.name)
      console.log(`PASS ${options.name}`)
    } catch (error) {
      receipt.failure = { scenario: options.name, message: String(error), errors, body: await page.locator('body').innerText() }
      await page.screenshot({ path: join(output, 'failure.png') })
      throw error
    } finally {
      for (const exchange of [...state.exchanges, ...state.previews, ...state.merges, ...state.mergePreviews]) exchange.gate?.resolve()
      await Promise.all([...state.exchanges, ...state.previews, ...state.merges, ...state.mergePreviews].map((exchange) => exchange.delivered.promise))
      await context.close()
      receipt.cleanup.contextsClosed++
      await owned.close()
      receipt.cleanup.browsersClosed++
      receipt.cleanup.profilesRemoved++
    }
  }

  async function capture(page, name) {
    await page.evaluate(() => document.fonts.ready)
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    assert.equal(await page.locator('main').evaluate((main) => main.scrollWidth <= main.clientWidth), true)
    const path = join(output, `${name}.png`)
    await page.screenshot({ path, animations: 'disabled' })
    const scrollers = await page.evaluate(() => [...document.querySelectorAll('*')]
      .filter((element) => /auto|scroll/.test(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight)
      .map((element) => ({ tag: element.tagName.toLowerCase(), className: element.className, top: element.scrollTop })))
    receipt.screenshots.push({ path, viewport: page.viewportSize(), scrollers })
  }

  async function close(completed) {
    await server.close()
    receipt.cleanup.viteClosed = true
    await rm(cacheDir, { recursive: true, force: true })
    receipt.cleanup.cacheRemoved = true
    await writeFile(join(output, 'receipt.json'), JSON.stringify({ ...receipt, completed }, null, 2))
    console.log(JSON.stringify({ ...receipt, screenshots: receipt.screenshots.length, requests: receipt.requests.length, completed, output }))
  }
  return { scenario, capture, close }
}

export const confirmation = (page, name = 'Operations') => page.getByRole('group', { name: `Delete “${name}”?`, exact: true })
export async function openUsers(page, base) {
  await page.goto(`${base}users?company=${tenant}`)
  await expect(page.getByRole('button', { name: 'Delete Operations', exact: true })).toBeVisible()
}
