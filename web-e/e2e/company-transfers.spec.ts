import { expect, test } from '@playwright/test'
import { admission, openTransferChoice, payment, prepareTransfers, target, tokenKey } from './company-transfers-fixture'

test('transfer choice traps focus, cancels safely and restores its trigger without submitting', async ({ page }) => {
  // Given
  const state = await prepareTransfers(page)
  await openTransferChoice(page)
  const dialog = page.getByRole('alertdialog')
  const cancel = dialog.getByRole('button', { name: 'Cancel', exact: true })
  await expect(cancel).toBeFocused()
  // When
  await page.keyboard.press('Tab')
  // Then
  await expect(dialog.getByRole('radio', { name: /^Paid/ })).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(cancel).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Transfer with invite', exact: true })).toBeFocused()
  expect(state.submissions).toEqual([])
})

for (const width of [390, 1440]) for (const theme of ['light', 'dark'] as const) {
  test(`free consent is explicit and stays contained at ${width}px in ${theme}`, async ({ page }, testInfo) => {
    // Given
    const state = await prepareTransfers(page)
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript((value) => localStorage.setItem('maraclaw-theme', value), theme)
    await openTransferChoice(page)
    const dialog = page.getByRole('alertdialog')
    await dialog.getByRole('radio', { name: /^Free/ }).check()
    const submit = dialog.getByRole('button', { name: 'Confirm free transfer', exact: true })
    await expect(submit).toBeDisabled()
    expect(state.submissions).toEqual([])
    await expect(dialog).toContainText('retained for compliance, not erased, and you lose access')
    await expect(dialog).toHaveAttribute('aria-modal', 'true')
    const geometry = await dialog.evaluate((element) => ({ width: element.getBoundingClientRect().width, scroll: element.scrollWidth, client: element.clientWidth }))
    expect(geometry.width).toBeLessThan(width)
    expect(geometry.scroll).toBe(geometry.client)
    await dialog.getByRole('checkbox').check()
    await page.screenshot({ path: testInfo.outputPath(`free-choice-${width}-${theme}.png`), fullPage: true })
    // When
    await submit.click()
    // Then
    await expect(dialog).toHaveCount(0)
    await expect(page.getByRole('status').filter({ hasText: 'Awaiting review' })).toBeVisible()
    expect(state.submissions).toEqual([{ password: 'synthetic-password', invitation_code: 'synthetic-invite', transfer_choice: 'free', free_destructive_consent: true }])
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
    expect(state.switches).toBe(0)
  })
}

for (const method of ['invite', 'domain'] as const) {
  test(`paid ${method} saves server policy and survives reload without installing a destination session`, async ({ page }, testInfo) => {
    // Given
    const state = await prepareTransfers(page)
    await openTransferChoice(page, method)
    const dialog = page.getByRole('alertdialog')
    await dialog.getByRole('radio', { name: /^Paid/ }).check()
    await expect(dialog).toContainText('¥123')
    await expect(dialog).toContainText('Synthetic current policy')
    // When
    await dialog.getByRole('button', { name: 'Request payment review', exact: true }).click()
    // Then
    await expect(page.getByRole('status').filter({ hasText: 'Awaiting payment review' })).toContainText('KWD')
    await expect(page.getByRole('status').filter({ hasText: 'Awaiting payment review' })).toContainText('1.234')
    expect(state.submissions).toEqual([{ password: 'synthetic-password', ...(method === 'invite' ? { invitation_code: 'synthetic-invite' } : { tenant_id: target.id }), transfer_choice: 'paid' }])
    await page.getByRole('link', { name: 'View transfer request', exact: true }).click()
    await page.reload()
    await expect(page.getByRole('region', { name: 'Saved transfer policy' })).toContainText(payment.instructions)
    await expect(page.getByRole('region', { name: 'Saved transfer policy' })).not.toContainText('Synthetic current policy')
    await expect(page.getByRole('region', { name: 'Transfer status' })).toContainText('Awaiting payment review')
    await expect(page.getByRole('button', { name: /^Open / })).toHaveCount(0)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
    expect(state.switches).toBe(0)
    await page.screenshot({ path: testInfo.outputPath(`payment-review-${method}.png`), fullPage: true })
  })
}

test('persisted transfer requests are discoverable and owner withdrawal survives reload', async ({ page }, testInfo) => {
  // Given
  const state = await prepareTransfers(page, { saved: payment })
  await page.goto('/app/admissions')
  await page.getByRole('link', { name: /^View transfer request/ }).click()
  // When
  await page.getByRole('button', { name: 'Withdraw transfer request', exact: true }).click()
  // Then
  await expect(page.getByRole('region', { name: 'Transfer status' })).toContainText('Transfer withdrawn')
  await page.reload()
  await expect(page.getByRole('region', { name: 'Transfer status' })).toContainText('Transfer withdrawn')
  await expect(page.getByRole('button', { name: 'Withdraw transfer request', exact: true })).toHaveCount(0)
  expect(state.transfer?.status).toBe('withdrawn')
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
  await page.screenshot({ path: testInfo.outputPath('withdrawn-transfer.png'), fullPage: true })
})

test('payment approval links to a still-pending destination admission, never opens the workspace', async ({ page }) => {
  // Given
  const state = await prepareTransfers(page, {
    saved: { ...payment, status: 'admission_created', payment_confirmed_at: payment.created_at, admission_request_id: admission.id },
    destination: { ...admission, transfer: { transfer_request_id: payment.id, choice: 'paid', fee_amount_minor: payment.fee_amount_minor, currency: payment.currency, free_consent_at: null } },
  })
  await page.goto(`/app/admissions/transfers/${payment.id}`)
  await expect(page.getByRole('region', { name: 'Transfer status' })).toContainText('Payment approved')
  // When
  await page.getByRole('link', { name: 'View destination admission', exact: true }).click()
  // Then
  await expect(page.getByRole('region', { name: 'Admission status' })).toContainText('Awaiting review')
  await expect(page.getByRole('button', { name: `Open ${target.name}` })).toHaveCount(0)
  expect(state.switches).toBe(0)
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
})

test('a non-default source cannot submit through either destination path', async ({ page }) => {
  // Given
  const state = await prepareTransfers(page, { eligible: false })
  await page.goto('/transfer')
  // When
  await page.getByLabel('Current password').fill('synthetic-password')
  await page.getByLabel('Invitation code', { exact: true }).fill('synthetic-invite')
  // Then
  await expect(page.getByRole('alert')).toContainText('Transfers from one company to another are not supported')
  await expect(page.getByRole('button', { name: 'Transfer with invite', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: `Transfer to ${target.name}`, exact: true })).toBeDisabled()
  expect(state.submissions).toEqual([])
})

test('a failed quote blocks transfer and can be retried without losing form values', async ({ page }) => {
  // Given
  const state = await prepareTransfers(page)
  await page.route('**/api/company-admissions/transfer-quote', (route) => route.fulfill({ status: 503, json: { detail: 'Synthetic quote unavailable' } }))
  await page.goto('/transfer')
  await page.getByLabel('Current password').fill('synthetic-password')
  await expect(page.getByRole('button', { name: 'Transfer with invite', exact: true })).toBeDisabled()
  await page.unroute('**/api/company-admissions/transfer-quote')
  // When
  await page.getByRole('button', { name: 'Retry transfer options', exact: true }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Transfer with invite', exact: true })).toBeEnabled()
  await expect(page.getByLabel('Current password')).toHaveValue('synthetic-password')
  expect(state.submissions).toEqual([])
})
