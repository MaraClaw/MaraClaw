import { expect, test, type Page } from '@playwright/test'

import type { AdmissionDetail } from '../src/lib/types/company-admissions'
import { accessEndpoint, member, pending, prepareAccess, tokenKey } from './admission-access-fixture'

const initialAdmission: AdmissionDetail = { ...pending, source_tenant_id: null, source_tenant: null, method: 'verified_domain' }

async function prepareJoin(page: Page, admission = initialAdmission) {
  const fixture = await prepareAccess(page, admission)
  await page.route('**/api/member/auth/me', (route) => route.fulfill({ json: { ...member, tenant_id: null } }))
  return fixture
}

test('join opens admissions when a tenantless initial application is pending', async ({ page }) => {
  // Given
  const { calls } = await prepareJoin(page)
  // When
  await page.goto('/join')
  // Then
  await expect(page).toHaveURL('/app/admissions')
  await expect(page.getByRole('link', { name: `View request for ${pending.target_tenant.name}` })).toBeVisible()
  expect(calls.filter((path) => path.startsWith('/api/tenants/join'))).toEqual([])
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('source-token')
})

for (const status of ['rejected', 'withdrawn'] as const) {
  test(`join preserves the chooser when the initial application is ${status}`, async ({ page }) => {
    // Given
    await prepareJoin(page, { ...initialAdmission, status })
    // When
    await page.goto('/join')
    // Then
    await expect(page.getByLabel('Invitation code', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Continue in default workspace', exact: true })).toBeEnabled()
    await expect(page).toHaveURL('/join')
  })
}

test('join preserves the chooser when no admission exists', async ({ page }) => {
  // Given
  await prepareJoin(page)
  await page.route(accessEndpoint, (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  // When
  await page.goto('/join#code=invitation-code')
  // Then
  await expect(page.getByLabel('Invitation code', { exact: true })).toHaveValue('invitation-code')
  await expect(page.getByRole('button', { name: 'Continue in default workspace', exact: true })).toBeEnabled()
})

test('join blocks its chooser while the admission lookup is unresolved', async ({ page }) => {
  // Given
  await prepareJoin(page)
  let release = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await page.route(accessEndpoint, async (route) => {
    await gate
    await route.fulfill({ json: { items: [], next_cursor: null } })
  })
  try {
    // When
    await page.goto('/join')
    // Then
    await expect(page.getByRole('status')).toBeVisible()
    await expect(page.getByLabel('Invitation code', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Continue in default workspace', exact: true })).toHaveCount(0)
  } finally {
    release()
  }
})

test('join blocks its chooser when the admission lookup fails', async ({ page }) => {
  // Given
  await prepareJoin(page)
  await page.route(accessEndpoint, (route) => route.fulfill({ status: 503, json: { detail: 'Admission lookup unavailable' } }))
  // When
  await page.goto('/join')
  // Then
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.getByLabel('Invitation code', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Continue in default workspace', exact: true })).toHaveCount(0)
  await expect(page).toHaveURL('/join')
})

test('join ignores a pending completion after its session owner logs out', async ({ page }) => {
  // Given
  await prepareJoin(page)
  let release = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await page.route(accessEndpoint, async (route) => {
    await gate
    await route.fulfill({ json: { items: [initialAdmission], next_cursor: null } })
  })
  await page.goto('/join')
  await expect(page.getByRole('status')).toBeVisible()
  try {
    // When
    await page.evaluate((key) => {
      localStorage.removeItem(key)
      window.dispatchEvent(new StorageEvent('storage', { key, newValue: null, storageArea: localStorage }))
    }, tokenKey)
    release()
    // Then
    await expect(page).toHaveURL('/login')
    await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible()
  } finally {
    release()
  }
})
