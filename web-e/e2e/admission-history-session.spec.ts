import { expect, test } from '@playwright/test'

import { historyCursor, historyPath, historyRows, prepareHistory, replacementRow } from './admission-history-fixture'
import { originalUser, replacementToken, tokenKey, writeFromOtherTab } from './session-fencing-fixture'

test('tenantless applicants can filter their persisted requests without entering membership', async ({ page }) => {
  // Given
  await prepareHistory(page)
  await page.route('**/api/auth/me', (route) => route.fulfill({ json: { ...originalUser, tenant_id: null } }))
  await page.goto('/app/admissions')
  await expect(page.getByRole('listitem')).toHaveCount(2)
  // When
  await page.getByLabel('Request status').selectOption('approved')
  // Then
  await expect(page.getByRole('heading', { name: 'History approved company' })).toBeVisible()
  await expect(page).toHaveURL('/app/admissions')
  await expect(page.getByRole('button', { name: /Open .*workspace/ })).toHaveCount(0)
})

test('changing a filter during a delayed older request never displays the retired page', async ({ page }) => {
  // Given
  await prepareHistory(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const delivered = Promise.withResolvers<void>()
  await page.route(`**${historyPath}*`, async (route) => {
    if (!new URL(route.request().url()).searchParams.has('cursor')) return route.fallback()
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { items: historyRows.slice(2), next_cursor: null } })
    delivered.resolve()
  })
  try {
    await page.goto('/app/admissions')
    await page.getByRole('button', { name: 'Older requests' }).click()
    await requested.promise
    await expect(page.getByRole('listitem')).toHaveCount(0)
    // When
    await page.getByLabel('Request status').selectOption('approved')
    await expect(page.getByRole('heading', { name: 'History approved company' })).toBeVisible()
    release.resolve()
    await delivered.promise
    // Then
    await expect(page.getByRole('listitem')).toHaveCount(1)
    await expect(page.getByRole('heading', { name: 'History rejected company' })).toHaveCount(0)
    await expect(page.getByText('Page 1', { exact: true })).toBeVisible()
  } finally {
    release.resolve()
  }
})

for (const status of [200, 401, 503]) {
  test(`delayed history ${status} cannot populate or expire a replacement owner`, async ({ page }) => {
    // Given
    const fixture = await prepareHistory(page)
    fixture.pages.set('pending:root', { items: historyRows.slice(0, 1), next_cursor: historyCursor })
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const delivered = Promise.withResolvers<void>()
    await page.route(`**${historyPath}*`, async (route) => {
      if (!new URL(route.request().url()).searchParams.has('cursor')) return route.fallback()
      requested.resolve()
      await release.promise
      await route.fulfill({ status, json: status === 200 ? { items: historyRows.slice(2), next_cursor: null } : { detail: 'Retired applicant' } })
      delivered.resolve()
    })
    try {
      await page.goto('/app/admissions')
      await page.getByLabel('Request status').selectOption('pending')
      await page.getByRole('button', { name: 'Older requests' }).click()
      await requested.promise
      // When
      await writeFromOtherTab(page, replacementToken)
      await expect(page.getByRole('heading', { name: replacementRow.target_tenant.name })).toBeVisible()
      release.resolve()
      await delivered.promise
      // Then
      await expect(page.getByLabel('Request status')).toHaveValue('all')
      await expect(page.getByText('Page 1', { exact: true })).toBeVisible()
      await expect(page.getByRole('listitem')).toHaveCount(1)
      await expect(page.getByRole('alert')).toHaveCount(0)
      expect(fixture.calls.at(-1)).toMatchObject({ status: null, cursor: null, token: `Bearer ${replacementToken}` })
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
    } finally {
      release.resolve()
    }
  })
}
