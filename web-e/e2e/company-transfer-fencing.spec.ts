import { expect, test } from '@playwright/test'
import { openTransferChoice, payment, prepareTransfers, tokenKey } from './company-transfers-fixture'
import { writeFromOtherTab } from './session-fencing-fixture'

for (const status of [202, 401, 503]) {
  test(`late transfer ${status} cannot update a replacement member's funnel`, async ({ page }) => {
    // Given
    await prepareTransfers(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const completed = Promise.withResolvers<void>()
    await page.route('**/api/tenants/transfer', async (route) => {
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 202 ? { status: 'payment_review', transfer_request: payment } : { detail: 'Obsolete source error' } })
      completed.resolve()
    })
    try {
      await openTransferChoice(page)
      await page.getByRole('radio', { name: /^Paid/ }).check()
      await page.getByRole('button', { name: 'Request payment review', exact: true }).click()
      await requested.promise
      // When
      await writeFromOtherTab(page, 'replacement-token')
      await expect(page.getByRole('alertdialog')).toHaveCount(0)
      release.resolve()
      await completed.promise
      // Then
      await expect(page.getByLabel('Current password')).toHaveCount(0)
      await expect(page.getByLabel('Invitation code', { exact: true })).toHaveValue('')
      await expect(page.getByRole('status').filter({ hasText: 'Awaiting payment review' })).toHaveCount(0)
      await expect(page.getByText('Obsolete source error', { exact: true })).toHaveCount(0)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('replacement-token')
    } finally { release.resolve() }
  })
}

test('a retired owner withdrawal cannot populate the replacement member status view', async ({ page }) => {
  // Given
  await prepareTransfers(page, { saved: payment })
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const completed = Promise.withResolvers<void>()
  await page.route(`**/api/company-admissions/transfers/${payment.id}/withdraw`, async (route) => {
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { ...payment, status: 'withdrawn', review_reason: 'Obsolete withdrawal result' } })
    completed.resolve()
  })
  try {
    await page.goto(`/app/admissions/transfers/${payment.id}`)
    await page.getByRole('button', { name: 'Withdraw transfer request', exact: true }).click()
    await requested.promise
    // When
    await writeFromOtherTab(page, 'replacement-token')
    await expect(page.getByRole('alert')).toContainText('Not found for replacement member')
    release.resolve()
    await completed.promise
    // Then
    await expect(page.getByRole('region', { name: 'Transfer status' })).toHaveCount(0)
    await expect(page.getByText('Obsolete withdrawal result', { exact: true })).toHaveCount(0)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('replacement-token')
  } finally { release.resolve() }
})
