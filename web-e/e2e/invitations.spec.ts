import { expect, test } from '@playwright/test'
import { admission, member, openTransferChoice, payment, prepareTransfers, target, tokenKey } from './company-transfers-fixture'
import { writeFromOtherTab } from './session-fencing-fixture'

for (const width of [390, 1440, 1920]) for (const theme of ['light', 'dark'] as const) {
  test(`invitation reveal and approved transfer at ${width}px ${theme} (synthetic HTTP)`, async ({ page }, testInfo) => {
    // Given
    const state = await prepareTransfers(page)
    await page.setViewportSize({ width, height: 1000 })
    await page.addInitScript((value) => localStorage.setItem('maraclaw-theme', value), theme)
    await page.goto('/transfer')
    await expect(page.getByRole('button', { name: 'Validate invitation', exact: true })).toBeDisabled()
    await expect(page.getByText(target.name, { exact: false })).toHaveCount(0)
    await expect(page.getByLabel('Current password')).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath(`after-code-${width}-${theme}.png`), fullPage: true })
    await page.getByLabel('Invitation code', { exact: true }).fill('invalid-code')
    await page.getByRole('button', { name: 'Validate invitation', exact: true }).click()
    await expect(page.getByRole('alert')).toHaveText('Invalid invitation code')
    await expect(page.getByText(target.name, { exact: false })).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath(`after-invalid-${width}-${theme}.png`), fullPage: true })
    await page.getByLabel('Invitation code', { exact: true }).fill('synthetic-invite')
    await page.getByRole('button', { name: 'Validate invitation', exact: true }).click()
    await expect(page.getByRole('status').filter({ hasText: 'Destination:' })).toContainText(target.name)
    await page.getByRole('button', { name: 'Continue transfer', exact: true }).click()
    await expect(page.getByText('Confirm your current password', { exact: true })).toBeVisible()
    await page.getByLabel('Current password').fill('synthetic-password')
    await page.screenshot({ path: testInfo.outputPath(`after-reveal-${width}-${theme}.png`), fullPage: true })
    await page.getByRole('button', { name: 'Continue transfer', exact: true }).click()
    await page.getByRole('radio', { name: /^Free/ }).check()
    await expect(page.getByRole('button', { name: 'Confirm free transfer', exact: true })).toBeDisabled()
    await page.getByRole('checkbox').check()
    await page.screenshot({ path: testInfo.outputPath(`after-choice-${width}-${theme}.png`), fullPage: true })
    const probe = await page.evaluate(() => {
      const dialog = document.querySelector('[role="alertdialog"]')
      return { documentOwner: document.scrollingElement?.tagName, viewport: innerWidth, documentWidth: document.documentElement.scrollWidth,
        dialogOverflowY: dialog ? getComputedStyle(dialog).overflowY : null, dialogWidth: dialog?.clientWidth, dialogScrollWidth: dialog?.scrollWidth }
    })
    expect(probe.documentWidth).toBe(width)
    expect(probe.dialogWidth).toBe(probe.dialogScrollWidth)
    expect(probe.dialogOverflowY).toBe('auto')
    await testInfo.attach('scroll-owner-probe', { body: JSON.stringify(probe), contentType: 'application/json' })
    // When
    await page.getByRole('button', { name: 'Confirm free transfer', exact: true }).click()
    // Then
    await expect(page).toHaveURL(/\/app\/agents$/)
    expect(state.switches).toBe(1)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('target-token')
    await page.screenshot({ path: testInfo.outputPath(`after-approved-${width}-${theme}.png`), fullPage: true })
  })
}

for (const retirement of ['code edit', 'session replacement'] as const) {
  test(`a stale preview cannot reveal the destination after ${retirement}`, async ({ page }) => {
    // Given
    await prepareTransfers(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const completed = Promise.withResolvers<void>()
    await page.route('**/api/tenants/invitations/preview', async route => {
      requested.resolve(); await release.promise
      await route.fulfill({ json: { tenant: { id: target.id, name: target.name } } }); completed.resolve()
    })
    try {
      await page.goto('/transfer')
      await page.getByLabel('Invitation code', { exact: true }).fill('synthetic-invite')
      await page.getByRole('button', { name: 'Validate invitation', exact: true }).click()
      await requested.promise
      await expect(page.getByRole('button', { name: 'Validating invitation…', exact: true })).toBeDisabled()
      // When
      if (retirement === 'code edit') await page.getByLabel('Invitation code', { exact: true }).fill('changed-code')
      else await writeFromOtherTab(page, 'replacement-token')
      release.resolve(); await completed.promise
      // Then
      await expect(page.getByLabel('Invitation code', { exact: true })).toHaveValue(retirement === 'code edit' ? 'changed-code' : '')
      await expect(page.getByText(target.name, { exact: false })).toHaveCount(0)
      await expect(page.getByLabel('Current password')).toHaveCount(0)
    } finally { release.resolve() }
  })
}

test('editing a validated code immediately removes its destination and confirmation controls', async ({ page }) => {
  // Given
  await prepareTransfers(page)
  await openTransferChoice(page)
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  // When
  await page.getByLabel('Invitation code', { exact: true }).fill('different-code')
  // Then
  await expect(page.getByText(target.name, { exact: false })).toHaveCount(0)
  await expect(page.getByLabel('Current password')).toHaveCount(0)
})

test('an invitation join enters the approved workspace without discovery or a second approval', async ({ page }) => {
  // Given
  const state = await prepareTransfers(page, { tenantless: true })
  await page.goto('/join')
  await expect(page.getByText(target.name, { exact: false })).toHaveCount(0)
  await page.getByLabel('Invitation code', { exact: true }).fill('synthetic-invite')
  await page.getByRole('button', { name: 'Validate invitation', exact: true }).click()
  // When
  await page.getByRole('button', { name: 'Join organization', exact: true }).click()
  // Then
  await expect(page).toHaveURL(/\/app\/agents$/)
  expect(state.switches).toBe(1)
})

test('an approved transfer waits for replacement identity validation before navigation', async ({ page }, testInfo) => {
  // Given
  await prepareTransfers(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/member/auth/me', async route => {
    if (route.request().headers().authorization !== 'Bearer target-token') return route.fallback()
    requested.resolve(); await release.promise
    await route.fulfill({ json: { ...member, display_name: 'Joined member', tenant_id: target.id } })
  })
  try {
    await openTransferChoice(page)
    await page.getByRole('radio', { name: /^Free/ }).check()
    await page.getByRole('checkbox').check()
    // When
    await page.getByRole('button', { name: 'Confirm free transfer', exact: true }).click()
    await requested.promise
    // Then
    await expect(page).toHaveURL('/transfer')
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('target-token')
    await expect(page.getByText('Organization joined', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('status')).toHaveText('Loading…')
    await page.screenshot({ path: testInfo.outputPath('after-opening-loading.png'), fullPage: true })
    release.resolve()
    await expect(page).toHaveURL(/\/app\/agents$/)
  } finally { release.resolve() }
})

test('a failed workspace switch can be retried without consuming the invitation twice', async ({ page }, testInfo) => {
  // Given
  const state = await prepareTransfers(page)
  await page.route('**/api/auth/switch-tenant', route => route.fulfill({ status: 503, json: { detail: 'Synthetic switch unavailable' } }))
  await openTransferChoice(page)
  await page.getByRole('radio', { name: /^Free/ }).check()
  await page.getByRole('checkbox').check()
  await page.getByRole('button', { name: 'Confirm free transfer', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open joined workspace', exact: true })).toBeEnabled()
  await page.screenshot({ path: testInfo.outputPath('after-switch-error.png'), fullPage: true })
  await page.unroute('**/api/auth/switch-tenant')
  // When
  await page.getByRole('button', { name: 'Open joined workspace', exact: true }).click()
  // Then
  await expect(page).toHaveURL(/\/app\/agents$/)
  expect(state.submissions).toHaveLength(1)
})

test('payment approval automatically opens the approved destination on saved transfer status', async ({ page }) => {
  // Given
  const state = await prepareTransfers(page, { saved: { ...payment, status: 'admission_created', admission_request_id: admission.id }, destination: { ...admission, status: 'approved', decided_at: admission.created_at } })
  // When
  await page.goto(`/app/admissions/transfers/${payment.id}`)
  // Then
  await expect(page).toHaveURL(/\/app\/agents$/)
  expect(state.switches).toBe(1)
})

test('an authenticated invitation link clears its fragment without validating automatically', async ({ page }) => {
  // Given
  await prepareTransfers(page)
  // When
  await page.goto('/transfer#code=synthetic-invite')
  // Then
  await expect(page).toHaveURL('/transfer')
  await expect(page.getByLabel('Invitation code', { exact: true })).toHaveValue('synthetic-invite')
  await expect(page.getByText(target.name, { exact: false })).toHaveCount(0)
})

for (const tenantless of [false, true]) {
  test(`an invitation link continues through password login ${tenantless ? 'into organization confirmation' : 'into transfer'} without persisting the code`, async ({ page }, testInfo) => {
    // Given
    await prepareTransfers(page, { anonymous: true, tenantless })
    await page.goto('/transfer#code=synthetic-invite')
    await expect(page).toHaveURL('/login')
    await page.getByLabel('Email', { exact: true }).fill(member.email)
    await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
    // When
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    // Then
    await expect(page).toHaveURL(tenantless ? '/join' : '/transfer')
    await expect(page.getByLabel('Invitation code', { exact: true })).toHaveValue('synthetic-invite')
    await expect(page.getByText(target.name, { exact: false })).toHaveCount(0)
    expect(await page.evaluate(() => Object.values(localStorage).includes('synthetic-invite') || Object.values(sessionStorage).includes('synthetic-invite'))).toBe(false)
    await page.screenshot({ path: testInfo.outputPath('after-login-invitation-continuation.png'), fullPage: true })
  })
}

test('a required password change gates invitation preview and transfer creation', async ({ page }) => {
  // Given
  await prepareTransfers(page, { mustChangePassword: true })
  // When
  await page.goto('/transfer#code=synthetic-invite')
  // Then
  await expect(page).toHaveURL('/app/settings')
  await expect(page.getByLabel('Invitation code', { exact: true })).toHaveCount(0)
  await expect(page.getByLabel('Display name')).toHaveValue(member.display_name)
})

for (const status of [200, 401, 503]) {
  test(`a late ${status} workspace switch cannot replace a newer session`, async ({ page }) => {
    // Given
    await prepareTransfers(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const completed = Promise.withResolvers<void>()
    await page.route('**/api/auth/switch-tenant', async route => {
      requested.resolve(); await release.promise
      await route.fulfill({ status, json: status === 200 ? { access_token: 'target-token' } : { detail: 'Retired switch error' } }); completed.resolve()
    })
    try {
      await openTransferChoice(page)
      await page.getByRole('radio', { name: /^Free/ }).check()
      await page.getByRole('checkbox').check()
      await page.getByRole('button', { name: 'Confirm free transfer', exact: true }).click()
      await requested.promise
      // When
      await writeFromOtherTab(page, 'replacement-token')
      release.resolve(); await completed.promise
      // Then
      await expect(page.getByLabel('Invitation code', { exact: true })).toHaveValue('')
      await expect(page.getByRole('alert')).toHaveCount(0)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('replacement-token')
      await expect(page).toHaveURL('/transfer')
    } finally { release.resolve() }
  })
}
