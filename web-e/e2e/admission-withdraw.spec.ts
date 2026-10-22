import { expect, test } from '@playwright/test'

import { admissionPage, admissionPath, pendingAdmission, prepareAdmission, withdrawnAdmission } from './admission-withdraw-fixture'
import { originalToken, replacementToken, replacementUser, tokenKey, writeFromOtherTab } from './session-fencing-fixture'

test('keyboard withdrawal refreshes persisted detail and list without replacing the session', async ({ page }) => {
  // Given
  const fixture = await prepareAdmission(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**${admissionPath}/withdraw`, async (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().postData()).toBeNull()
    requested.resolve()
    await release.promise
    fixture.setDetail(withdrawnAdmission)
    await route.fulfill({ json: withdrawnAdmission })
  })
  await page.goto(admissionPage)
  const button = page.getByRole('button', { name: 'Withdraw request', exact: true })
  await button.focus()
  // When
  await button.press('Enter')
  await requested.promise
  await expect(page.getByRole('button', { name: 'Withdrawing request…' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Refresh status' })).toBeDisabled()
  release.resolve()
  // Then
  await expect(page.getByRole('region', { name: 'Admission status' }).getByText('Withdrawn', { exact: true })).toBeVisible()
  await expect(page.locator(`time[datetime="${withdrawnAdmission.decided_at}"]`)).toBeVisible()
  await expect(page.getByRole('button', { name: /Withdraw request|Open Fencing/ })).toHaveCount(0)
  expect(fixture.calls.filter((call) => call === `GET ${admissionPath}`).length).toBeGreaterThan(1)
  await page.getByRole('link', { name: 'All admission requests' }).click()
  await expect(page).toHaveURL('/app/admissions')
  await expect(page.getByRole('listitem').getByText('Withdrawn', { exact: true })).toBeVisible()
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
})

for (const status of ['approved', 'rejected', 'withdrawn'] as const) {
  test(`${status} requests have no withdrawal action after reload`, async ({ page }) => {
    // Given
    await prepareAdmission(page, { ...pendingAdmission, status, decided_at: withdrawnAdmission.decided_at })
    // When
    await page.goto(admissionPage)
    // Then
    await expect(page.getByRole('region', { name: 'Admission status' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Withdraw request', exact: true })).toHaveCount(0)
  })
}

test('a review race reports conflict and refreshes the winning decision without switching membership', async ({ page }) => {
  // Given
  const fixture = await prepareAdmission(page)
  await page.route(`**${admissionPath}/withdraw`, async (route) => {
    fixture.setDetail({ ...pendingAdmission, status: 'approved', decided_at: withdrawnAdmission.decided_at })
    await route.fulfill({ status: 409, json: { detail: 'Request already approved' } })
  })
  await page.goto(admissionPage)
  // When
  await page.getByRole('button', { name: 'Withdraw request' }).click()
  // Then
  await expect(page.getByRole('alert')).toHaveText('Request already approved')
  await expect(page.getByText('Approved', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Withdraw request' })).toHaveCount(0)
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
})

test('a failed withdrawal keeps pending status and offers a safe retry', async ({ page }) => {
  // Given
  const fixture = await prepareAdmission(page)
  let attempts = 0
  await page.route(`**${admissionPath}/withdraw`, async (route) => {
    if (++attempts === 1) return route.fulfill({ status: 503, json: {} })
    fixture.setDetail(withdrawnAdmission)
    await route.fulfill({ json: withdrawnAdmission })
  })
  await page.goto(admissionPage)
  await page.getByRole('button', { name: 'Withdraw request' }).click()
  await expect(page.getByRole('alert')).toContainText('Unable to withdraw')
  await expect(page.getByText('Awaiting review', { exact: true })).toBeVisible()
  // When
  await page.getByRole('button', { name: 'Withdraw request' }).click()
  // Then
  await expect(page.getByRole('region', { name: 'Admission status' }).getByText('Withdrawn', { exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

for (const status of [200, 401, 503]) {
  test(`late withdrawal ${status} cannot refresh or expire a replacement owner`, async ({ page }) => {
    // Given
    const fixture = await prepareAdmission(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route(`**${admissionPath}/withdraw`, async (route) => {
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200 ? withdrawnAdmission : { detail: 'Retired applicant owner' } })
    })
    await page.goto(admissionPage)
    await page.getByRole('button', { name: 'Withdraw request' }).click()
    await requested.promise
    await writeFromOtherTab(page, replacementToken)
    await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
    await expect(page.getByLabel('Display name')).toHaveValue(replacementUser.display_name ?? '')
    const before = fixture.calls.length
    const completed = page.waitForEvent('requestfinished', { predicate: (request) => request.url().endsWith('/withdraw') })
    // When
    release.resolve()
    await completed
    // Then
    await expect(page).toHaveURL('/app/settings')
    await expect(page.getByText(/Retired applicant owner|Unable to withdraw/)).toHaveCount(0)
    expect(fixture.calls).toHaveLength(before)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
  })
}
