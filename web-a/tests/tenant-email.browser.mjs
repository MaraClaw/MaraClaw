import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { createEmailHarness, tenantA, tenantB } from './tenant-email-browser-harness.mjs'

async function replaceMembership(page, state, tenant) {
  state.user = { ...state.user, tenant_id: tenant }
  await page.evaluate(() => {
    localStorage.setItem('maraclaw-admin-token', 'replacement-synthetic-token')
    window.dispatchEvent(new StorageEvent('storage', { key: 'maraclaw-admin-token', newValue: 'replacement-synthetic-token', storageArea: localStorage }))
  })
}

const harness = await createEmailHarness()
try {
  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) {
    const getGate = Promise.withResolvers()
    await harness.scenario({ name: `${width}-${scheme}-after`, width, scheme, getGate, getStatus: 503 }, async (page, state, capture) => {
      const section = page.getByRole('region', { name: 'Organization SMTP', exact: true })
      // Given: a genesis PA is restricted to its own MaraClaw membership, without a picker.
      await expect(section.getByRole('combobox')).toHaveCount(0)
      await expect(section.getByText('Loading SMTP settings…', { exact: true })).toBeVisible()
      await capture('loading')
      getGate.resolve()
      await expect(section.getByRole('alert')).toBeVisible()
      await capture('load-error')
      state.getGate = null; state.getStatus = 200
      await section.getByRole('button', { name: 'Retry loading SMTP settings' }).click()
      const sender = section.getByLabel('Sender email address', { exact: true })
      const name = section.getByLabel('Sender name', { exact: true })
      const password = section.getByLabel('SMTP password', { exact: true })
      const save = section.getByRole('button', { name: 'Save SMTP settings', exact: true })
      await expect(sender).toHaveValue('mail@example.test')
      await expect(password).toHaveValue('')
      await expect(section.getByText(/Your organization: MaraClaw/)).toBeVisible()
      assert.deepEqual(await page.locator('main').getByRole('heading', { name: /^(Settings|Organization SMTP|Transfer fee and instructions|Session)$/ }).allTextContents(), ['Settings', 'Organization SMTP', 'Transfer fee and instructions', 'Session'])
      await page.locator('main').evaluate((element) => { element.scrollTop = 0 })
      await capture('top')
      await page.locator('main').evaluate((element) => { element.scrollTop = element.scrollHeight })
      await capture('end')
      await save.scrollIntoViewIfNeeded(); await capture('smtp-actions')
      await sender.fill('not-an-email'); await save.click()
      await expect(sender).toBeFocused(); await expect(sender).toHaveAttribute('aria-invalid', 'true')
      assert.equal(state.requests.filter(({ method }) => method === 'PUT').length, 0)
      await capture('validation')
      await sender.fill('new@example.test'); await name.fill('Updated sender'); await password.fill('synthetic-only-secret')
      state.putStatus = 422; state.putGate = Promise.withResolvers()
      // When: duplicate submits race before React paints pending state.
      await save.evaluate((button) => { button.click(); button.click() })
      await expect(section.getByRole('button', { name: 'Saving SMTP settings…' })).toBeDisabled()
      await expect(sender).toBeDisabled(); await expect(password).toBeDisabled()
      assert.equal(state.requests.filter(({ method }) => method === 'PUT').length, 1)
      await capture('save-pending')
      state.putGate.resolve()
      // Then: failure retains the local draft; success clears plaintext and persists across reload.
      await expect(section.getByRole('alert')).toContainText('422')
      await expect(name).toHaveValue('Updated sender'); await expect(password).toHaveValue('synthetic-only-secret')
      await capture('save-error')
      state.putStatus = 200; state.putGate = null
      await save.click()
      await expect(section.getByRole('status')).toContainText('SMTP settings saved.')
      await expect(password).toHaveValue('')
      await capture('save-success')
      await page.reload()
      await expect(sender).toHaveValue('new@example.test'); await expect(password).toHaveValue('')
      await name.fill('Unsaved draft')
      await section.getByRole('button', { name: 'Send test email', exact: true }).click()
      await expect(section.getByRole('status')).toContainText('operator@example.test')
      const sent = state.requests.find(({ method }) => method === 'POST')
      assert.equal(sent.tenant, tenantA); assert.equal(sent.body, null)
      assert.equal(state.configs[tenantA].from_name, 'Updated sender')
      await capture('test-success')
      state.testStatus = 503
      await section.getByRole('button', { name: 'Send test email', exact: true }).click()
      await expect(section.getByRole('alert')).toContainText('503')
      await capture('test-error')
      await section.getByLabel('Enable organization SMTP', { exact: true }).uncheck()
      await save.click()
      await expect(section.getByText(/Organization email delivery is off\. Save and enable/)).toBeVisible()
      state.testStatus = 200
      await section.getByRole('button', { name: 'Send test email', exact: true }).click()
      await expect(section.getByRole('status')).toContainText('operator@example.test')
      assert.equal(state.configs[tenantA].enabled, false)
      assert.equal(state.configs[tenantA].effective_source, 'none')
      await capture('disabled')
      page.once('dialog', (dialog) => { assert.match(dialog.message(), /delivery will stop/); return dialog.dismiss() })
      await section.getByRole('button', { name: 'Remove SMTP configuration' }).click()
      assert.equal(state.requests.filter(({ method }) => method === 'DELETE').length, 0)
      page.once('dialog', (dialog) => dialog.accept())
      await section.getByRole('button', { name: 'Remove SMTP configuration' }).click()
      await expect(section.getByRole('status')).toContainText('configuration removed')
      await expect(sender).toHaveValue(''); await expect(name).toHaveValue(''); await expect(password).toHaveValue('')
      await expect(section.getByLabel('SMTP port', { exact: true })).toHaveValue('465')
      await expect(section.getByLabel('Timeout (seconds)', { exact: true })).toHaveValue('15')
      await expect(section.getByLabel('Use implicit TLS (SSL)', { exact: true })).toBeChecked()
      assert.equal(state.configs[tenantA].has_password, false)
      assert.equal(state.configs[tenantA].effective_source, 'none')
      assert.ok(state.requests.every(({ tenant }) => tenant === tenantA))
      assert.ok(state.auxiliaryRequests.every((path) => path === `/api/tenants/${tenantA}`))
      await capture('removed')
    })
  }
  for (const [name, user] of [
    ['additional-oa', { role: 'org_admin', is_platform_admin: false, is_genesis: false }],
    ['additional-pa', { is_genesis: false }],
    ['missing-genesis', { is_genesis: undefined }],
    ['missing-tenant', { tenant_id: null }],
    ['inactive', { is_active: false }],
    ['nonadmin-with-platform-flag', { role: 'member' }],
  ]) await harness.scenario({ name, user }, async (page, state, capture) => {
    // Given / When: a disallowed membership opens Settings.
    await expect(page.getByRole('heading', { name: 'Session', exact: true })).toBeVisible()
    // Then: hide the entire card and issue no SMTP, tenant or company request.
    await expect(page.getByRole('region', { name: 'Organization SMTP', exact: true })).toHaveCount(0)
    assert.equal(state.requests.length, 0); assert.equal(state.auxiliaryRequests.length, 0)
    await capture('hidden')
  })
  for (const role of ['org_admin', 'platform_admin']) await harness.scenario({ name: `forced-change-${role}`, user: { role, is_platform_admin: role === 'platform_admin', must_change_password: true } }, async (page, state, capture) => {
    // Given / When: an eligible genesis administrator must change their password.
    const section = page.getByRole('region', { name: 'Organization SMTP', exact: true })
    await expect(section.getByRole('link', { name: 'Account' })).toBeVisible()
    // Then
    await expect(section.getByRole('textbox')).toHaveCount(0); await expect(section.getByRole('button')).toHaveCount(0)
    assert.equal(state.requests.length, 0); assert.equal(state.auxiliaryRequests.length, 0)
    await capture('blocked')
  })
  for (const role of ['org_admin', 'platform_admin']) await harness.scenario({ name: `openclaw-genesis-${role}`, user: { role, is_platform_admin: role === 'platform_admin', tenant_id: tenantB } }, async (page, state, capture) => {
    // Given: the genesis administrator belongs to OpenClaw, not MaraClaw.
    const section = page.getByRole('region', { name: 'Organization SMTP', exact: true })
    await expect(section.getByText(/Your organization: OpenClaw/)).toBeVisible()
    await expect(section.getByRole('combobox')).toHaveCount(0)
    await expect(section.getByLabel('SMTP host', { exact: true })).toHaveValue('')
    await expect(section.getByLabel('Sender name', { exact: true })).toHaveValue('')
    // When
    await section.getByRole('button', { name: 'Save SMTP settings', exact: true }).click()
    // Then
    await expect(section.getByRole('status')).toContainText('saved')
    assert.ok(state.requests.every(({ tenant }) => tenant === tenantB))
    assert.ok(state.auxiliaryRequests.includes(`/api/tenants/${tenantB}`))
    assert.ok(state.auxiliaryRequests.every((path) => path === '/api/tenants/me' || path === `/api/tenants/${tenantB}`))
    assert.equal('password' in state.requests.find(({ method }) => method === 'PUT').body, false)
    assert.equal(state.requests.find(({ method }) => method === 'PUT').body.from_name, '')
    await capture('own-only')
  })
  await harness.scenario({ name: 'genesis-oa-mobile-keyboard', width: 390, user: { role: 'org_admin', is_platform_admin: false } }, async (page, state, capture) => {
    // Given
    const section = page.getByRole('region', { name: 'Organization SMTP', exact: true })
    await expect(section.getByText(/Your organization: MaraClaw/)).toBeVisible()
    const sender = section.getByLabel('Sender email address', { exact: true })
    await sender.focus()
    // When
    await page.keyboard.press('Tab')
    // Then
    await expect(section.getByLabel('Sender name', { exact: true })).toBeFocused()
    await expect(sender).toHaveAttribute('inputmode', 'email')
    await expect(section.getByLabel('SMTP port', { exact: true })).toHaveAttribute('inputmode', 'numeric')
    assert.ok(state.requests.every(({ tenant }) => tenant === tenantA))
    await capture('keyboard-focus')
  })
  for (const operation of ['save', 'read']) await harness.scenario({ name: `membership-change-${operation}`, getGate: operation === 'read' ? Promise.withResolvers() : null }, async (page, state, capture) => {
    // Given: the outgoing membership has an operation in flight.
    const section = page.getByRole('region', { name: 'Organization SMTP', exact: true })
    if (operation === 'save') {
      await section.getByLabel('Sender name', { exact: true }).fill('Outgoing membership draft')
      await section.getByLabel('SMTP password', { exact: true }).fill('outgoing-synthetic-secret')
      state.putGate = Promise.withResolvers()
      await section.getByRole('button', { name: 'Save SMTP settings', exact: true }).click()
      await expect(section.getByRole('button', { name: 'Saving SMTP settings…' })).toBeDisabled()
    } else await expect(section.getByText('Loading SMTP settings…')).toBeVisible()
    const held = state.requests.find(({ method }) => method === (operation === 'save' ? 'PUT' : 'GET'))
    state.getGate = null
    // When: a new authenticated own membership replaces the old session.
    await replaceMembership(page, state, tenantB)
    await expect(section.getByText(/Your organization: OpenClaw/)).toBeVisible()
    held.gate.resolve(); await held.delivered.promise
    // Then: old values, plaintext and late status cannot enter the new form.
    await expect(section.getByLabel('Sender name', { exact: true })).toHaveValue('')
    await expect(section.getByLabel('SMTP password', { exact: true })).toHaveValue('')
    await expect(section.getByRole('status')).toBeEmpty()
    await capture('late-operation-isolated')
  })
  await harness.scenario({ name: 'retired-smtp-session' }, async (page, state) => {
    // Given
    const section = page.getByRole('region', { name: 'Organization SMTP', exact: true })
    await section.getByLabel('Sender name', { exact: true }).fill('Pending logout')
    state.putGate = Promise.withResolvers()
    await section.getByRole('button', { name: 'Save SMTP settings', exact: true }).click()
    await expect(section.getByRole('button', { name: 'Saving SMTP settings…' })).toBeDisabled()
    // When
    await page.getByRole('main').getByRole('button', { name: 'Sign out', exact: true }).click()
    state.putGate.resolve(); await state.requests.find(({ method }) => method === 'PUT').delivered.promise
    // Then
    await expect(page).toHaveURL(/\/login$/)
    await expect(page.getByText('SMTP settings saved.', { exact: true })).toHaveCount(0)
  })
} finally { await harness.close() }
