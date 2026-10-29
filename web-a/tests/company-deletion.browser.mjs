import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { chromium, expect } from '@playwright/test'
import { createServer } from 'vite'
import { assertReducedPress, captureCompanyDeletionEvidence, captureCompanyNameStress } from './company-deletion-browser-evidence.mjs'
import { companyDeletionAuthorizationScenarios } from './company-deletion-authorization.browser.mjs'

const output = process.env.COMPANY_PREVIEW_ARTIFACTS ?? join(process.env.TMPDIR, 'company-preview-artifacts')
await mkdir(output, { recursive: true })
const server = await createServer({ server: { host: '127.0.0.1', port: 0, watch: { ignored: (path) => path.startsWith(output) } } })
await server.listen()
const base = server.resolvedUrls.local[0]
const browser = await chromium.launch({ headless: true, args: ['--remote-debugging-port=0', '--enable-automation'] })
const evidence = []
const counts = { users_count: 7, agents_count: 3, schedules_count: 11 }
const company = (id, name, extra = {}) => ({
  id, name, slug: id, is_active: true, sso_enabled: false, sso_domain: null,
  is_system: false, is_default_end_user_org: false, can_disable: true, created_at: null,
  user_count: 20, active_user_count: 19, inactive_user_count: 1, agent_count: 8,
  org_admin_email: null, ...extra,
})
const initial = [company('alpha', 'Alpha Labs'), company('beta', 'Beta Works'), company('system', 'MaraClaw', { is_system: true }), company('default', 'KABERs', { is_default_end_user_org: true })]
const admin = {
  id: 'operator', identity_id: 'identity', email: 'operator@example.test',
  display_name: 'Operator', avatar_url: null, role: 'platform_admin', is_platform_admin: true,
  tenant_id: 'system', is_active: true, email_verified: true, must_change_password: false,
  created_at: '2026-10-20T00:00:00Z',
}

async function scenario(name, exercise) {
  const context = await browser.newContext({ viewport: { width: 1365, height: 1100 } })
  const state = {
    companies: [...initial], previewStatus: 200, deleteStatus: 200, role: 'platform_admin',
    previewCalls: 0, deleteCalls: 0, listCalls: 0, previewGate: null, deleteGate: null,
    previewStarted: Promise.withResolvers(), deleteStarted: Promise.withResolvers(),
    previewDelivered: Promise.withResolvers(), deleteDelivered: Promise.withResolvers(),
  }
  const errors = []
  try {
    await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-token'))
    const page = await context.newPage()
    page.on('pageerror', (error) => errors.push(error.message))
    await page.route('**/api/**', async (route) => {
      const path = new URL(route.request().url()).pathname
      let body
      let status = 200
      if (path === '/api/auth/me') body = { ...admin, role: state.role, is_platform_admin: state.role === 'platform_admin' }
      else if (path === '/api/auth/login') body = { access_token: 'synthetic-token', token_type: 'bearer', user: admin }
      else if (path === '/api/admin/companies') { state.listCalls++; body = state.companies }
      else if (path.endsWith('/email-domains')) body = []
      else if (path.endsWith('/deletion-preview')) {
        state.previewCalls++
        state.previewStarted.resolve()
        const capturedStatus = state.previewStatus
        if (state.previewGate) await state.previewGate.promise
        status = capturedStatus
        body = status === 200 ? counts : { detail: 'Preview access rejected' }
      } else if (route.request().method() === 'DELETE') {
        state.deleteCalls++
        state.deleteStarted.resolve()
        const capturedStatus = state.deleteStatus
        if (state.deleteGate) await state.deleteGate.promise
        status = capturedStatus
        if (status === 200) state.companies = state.companies.filter((item) => item.id !== path.split('/').at(-1))
        body = status === 200 ? { status: 'deleted', fallback_tenant_id: null } : { detail: 'Company is now protected' }
      } else throw new Error(`Unexpected API ${route.request().method()} ${path}`)
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
      if (path.endsWith('/deletion-preview')) state.previewDelivered.resolve()
      if (route.request().method() === 'DELETE') state.deleteDelivered.resolve()
    })
    await page.goto(`${base}companies/alpha`)
    await exercise(page, state)
    assert.deepEqual(errors, [])
    evidence.push(name)
    console.log(`PASS ${name}`)
  } finally {
    state.previewGate?.resolve()
    state.deleteGate?.resolve()
    await context.close()
  }
}

async function preview(page) {
  await page.getByRole('button', { name: 'Preview deletion' }).click()
  await expect(page.getByLabel('Enter Alpha Labs to confirm')).toBeFocused()
}

async function switchCompany(page) {
  await page.getByRole('link', { name: 'Companies', exact: true }).first().click()
  await page.getByRole('link', { name: 'Open Beta Works' }).click()
  await expect(page.getByRole('heading', { name: 'Beta Works' })).toBeVisible()
}

async function replaceSameToken(page) {
  await page.getByRole('button', { name: 'Sign out', exact: true }).click()
  await page.getByLabel('Email', { exact: true }).fill('operator@example.test')
  await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Preview deletion' })).toBeVisible()
}

try {
  await companyDeletionAuthorizationScenarios(scenario)
  await scenario('advisory preview, exact name, duplicate prevention and successful deletion', async (page, state) => {
    // Given
    assert.equal(state.previewCalls, 0)
    state.deleteGate = Promise.withResolvers()
    await page.getByRole('button', { name: 'Preview deletion' }).scrollIntoViewIfNeeded()
    await page.screenshot({ path: join(output, 'company-preview-closed.png') })
    await preview(page)
    assert.equal(state.deleteCalls, 0)
    await expect(page.locator('dl')).toContainText('7')
    await expect(page.locator('dl')).toContainText('3')
    await expect(page.locator('dl')).toContainText('11')
    const input = page.getByLabel('Enter Alpha Labs to confirm')
    const submit = page.getByRole('button', { name: 'Delete company permanently' })
    for (const value of ['alpha labs', ' Alpha Labs', 'Alpha Labs ']) {
      await input.fill(value)
      await expect(submit).toBeDisabled()
    }
    await page.screenshot({ path: join(output, 'company-preview-name-mismatch.png') })
    await input.fill('Alpha Labs')
    await input.press('Tab')
    await expect(submit).toBeFocused()
    await captureCompanyDeletionEvidence(page, browser, output)
    // When
    await submit.evaluate((button) => { button.click(); button.click() })
    await state.deleteStarted.promise
    await expect(page.getByRole('button', { name: 'Deleting company' })).toBeDisabled()
    await page.screenshot({ path: join(output, 'company-preview-delete-pending.png'), animations: 'disabled' })
    assert.equal(state.deleteCalls, 1)
    state.deleteGate.resolve()
    // Then
    await expect(page).toHaveURL(`${base}companies`)
    await expect(page.getByText('Company deleted', { exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Open Alpha Labs' })).toHaveCount(0)
    assert.ok(state.listCalls >= 2)
    assert.equal(state.previewCalls, 1)
  })

  await scenario('preview failure blocks confirmation and supports retry', async (page, state) => {
    // Given
    state.previewStatus = 403
    // When
    await page.getByRole('button', { name: 'Preview deletion' }).click()
    // Then
    await expect(page.getByRole('alert')).toContainText('Preview failed:')
    await page.screenshot({ path: join(output, 'company-preview-error.png') })
    await expect(page.getByRole('button', { name: 'Delete company permanently' })).toHaveCount(0)
    await expect(page.getByLabel('Enter Alpha Labs to confirm')).toHaveCount(0)
    assert.equal(state.deleteCalls, 0)
    state.previewStatus = 200
    await page.getByRole('button', { name: 'Retry preview' }).click()
    await expect(page.getByLabel('Enter Alpha Labs to confirm')).toBeFocused()
  })

  await scenario('keyboard preview focus, explicit confirmation and reduced-motion presses', async (page, state) => {
    // Given
    state.previewGate = Promise.withResolvers()
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await assertReducedPress(page, page.getByRole('button', { name: 'Preview deletion' }), join(output, 'company-preview-reduced-motion-pressed.png'))
    await page.getByRole('button', { name: 'Preview deletion' }).click()
    await state.previewStarted.promise
    await expect(page.getByRole('status')).toBeFocused()
    await page.screenshot({ path: join(output, 'company-preview-loading.png') })
    state.previewGate.resolve()
    const input = page.getByLabel('Enter Alpha Labs to confirm')
    const submit = page.getByRole('button', { name: 'Delete company permanently' })
    await expect(input).toBeFocused()
    await input.fill('Alpha Labs')
    await assertReducedPress(page, submit, join(output, 'company-delete-reduced-motion-pressed.png'))
    // When
    await input.focus()
    await input.press('Enter')
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    // Then
    assert.equal(state.deleteCalls, 0)
    await expect(input).toBeFocused()
    await input.press('Tab')
    await expect(submit).toBeFocused()
    await submit.press('Enter')
    await expect(page).toHaveURL(`${base}companies`)
    assert.equal(state.deleteCalls, 1)
  })

  await scenario('deletion failure preserves preview and exact-name confirmation', async (page, state) => {
    // Given
    state.deleteStatus = 400
    await preview(page)
    await page.getByLabel('Enter Alpha Labs to confirm').fill('Alpha Labs')
    // When
    await page.getByRole('button', { name: 'Delete company permanently' }).click()
    // Then
    await expect(page.getByRole('alert')).toContainText('Deletion failed:')
    await page.screenshot({ path: join(output, 'company-deletion-error.png') })
    await expect(page.getByRole('alert')).not.toContainText('Preview failed:')
    await expect(page.getByLabel('Enter Alpha Labs to confirm')).toHaveValue('Alpha Labs')
    await expect(page.locator('dl')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Delete company permanently' })).toBeEnabled()
    assert.equal(state.deleteCalls, 1)
  })

  await scenario('long company names and 200 percent text zoom retain confirmation controls', async (page, state) => {
    // Given
    state.companies = state.companies.map((item) => item.id === 'alpha' ? { ...item, name: 'CompanyNameWithoutBreaks'.repeat(5) } : item)
    // When / Then
    await page.reload()
    await captureCompanyNameStress(page, output)
    assert.equal(state.deleteCalls, 0)
  })

  for (const operation of ['preview', 'delete']) for (const replacement of ['company', 'same-token session']) for (const status of [200, 403]) {
    await scenario(`late ${operation} ${status} cannot affect replacement ${replacement}`, async (page, state) => {
      // Given
      const gate = Promise.withResolvers()
      if (operation === 'preview') { state.previewGate = gate; state.previewStatus = status }
      else { await preview(page); state.deleteGate = gate; state.deleteStatus = status }
      if (operation === 'preview') await page.getByRole('button', { name: 'Preview deletion' }).click()
      else {
        await page.getByLabel('Enter Alpha Labs to confirm').fill('Alpha Labs')
        await page.getByRole('button', { name: 'Delete company permanently' }).click()
      }
      await state[`${operation}Started`].promise
      // When
      if (replacement === 'company') await switchCompany(page)
      else await replaceSameToken(page)
      const listCalls = state.listCalls
      gate.resolve()
      await state[`${operation}Delivered`].promise
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
      // Then
      await expect(page).toHaveURL(`${base}companies/${replacement === 'company' ? 'beta' : 'alpha'}`)
      await expect(page.getByRole('button', { name: 'Preview deletion' })).toBeVisible()
      await expect(page.getByRole('alert')).toHaveCount(0)
      await expect(page.getByText('Company deleted', { exact: true })).toHaveCount(0)
      await expect(page.locator('dl')).toHaveCount(0)
      assert.equal(state.listCalls, listCalls)
    })
  }

  for (const [id, name] of [['system', 'MaraClaw'], ['default', 'KABERs']]) await scenario(`protected ${id} company does not offer deletion`, async (page) => {
    // Given / When
    await page.goto(`${base}companies/${id}`)
    // Then
    await expect(page.getByRole('heading', { name })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Preview deletion' })).toHaveCount(0)
  })
  await scenario('organization admin cannot open the company deletion surface', async (page, state) => {
    // Given
    state.role = 'org_admin'
    // When
    await page.reload()
    // Then
    await expect(page).toHaveURL(base)
    await expect(page.getByRole('button', { name: 'Preview deletion' })).toHaveCount(0)
    assert.equal(state.previewCalls, 0)
    assert.equal(state.deleteCalls, 0)
  })
  await writeFile(join(output, 'receipt.json'), JSON.stringify({ passed: evidence.length, scenarios: evidence, output, cleanup: 'Contexts, skill connection, Chromium and Vite are closed in finally blocks.' }, null, 2))
  console.log(JSON.stringify({ passed: evidence.length, scenarios: evidence, output }))
} finally {
  await browser.close()
  await server.close()
}
