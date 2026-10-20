import { expect, test } from '@playwright/test'

import { directoryPath, localDepartment, prepareLocalDepartments, siblingDepartment } from './local-departments-fixture'
import { registerLocalDepartmentRosterTests } from './local-department-rosters-cases'
import { originalCompany } from './session-fencing-fixture'

for (const role of ['member', 'agent_admin']) {
  test(`${role} sees readonly local department cards`, async ({ page }) => {
    // Given
    const state = await prepareLocalDepartments(page, role)
    // When
    await page.goto(directoryPath)
    // Then
    const local = page.getByRole('region', { name: 'Local departments', exact: true })
    await expect(local.getByRole('heading', { name: localDepartment.name })).toBeVisible()
    await expect(local.getByRole('button', { name: /^Edit name/ })).toHaveCount(0)
    expect(state.privilegedRequests).toBe(0)
  })
}

test('org admin saves a trimmed name with Enter and sees the refreshed card', async ({ page }) => {
  // Given
  const state = await prepareLocalDepartments(page, 'org_admin')
  await page.goto(directoryPath)
  await page.getByRole('button', { name: 'Edit name for Engineering', exact: true }).click()
  const input = page.getByLabel('Department name', { exact: true })
  await expect(input).toBeFocused()
  await input.fill('  Product engineering  ')
  // When
  await input.press('Enter')
  // Then
  const edit = page.getByRole('button', { name: 'Edit name for Product engineering', exact: true })
  await expect(edit).toBeFocused()
  await expect(page.getByRole('heading', { name: 'Product engineering', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: siblingDepartment.name, exact: true })).toBeVisible()
  expect(state.patches).toEqual([{ name: 'Product engineering' }])
  expect(state.optionsRequests).toBe(2)
  expect(state.privilegedRequests).toBe(0)
})

registerLocalDepartmentRosterTests()

test('successful rename restores focus while the list refresh is still pending', async ({ page }) => {
  // Given
  const state = await prepareLocalDepartments(page, 'org_admin')
  await page.goto(directoryPath)
  await page.getByRole('button', { name: 'Edit name for Engineering', exact: true }).click()
  await page.getByLabel('Department name').fill('Product engineering')
  const refreshStarted = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/departments/options', async (route) => {
    refreshStarted.resolve()
    await release.promise
    await route.fulfill({ json: { departments: state.departments } })
  })
  try {
    // When
    await page.getByLabel('Department name').press('Enter')
    await refreshStarted.promise
    // Then
    const edit = page.getByRole('button', { name: 'Edit name for Product engineering', exact: true })
    await expect(edit).toBeEnabled()
    await expect(edit).toBeFocused()
    await expect(page.getByRole('heading', { name: siblingDepartment.name, exact: true })).toBeVisible()
  } finally {
    release.resolve()
  }
})

test('failed list refresh keeps the saved name and sibling cards with inline retry', async ({ page }) => {
  // Given
  await prepareLocalDepartments(page, 'org_admin')
  await page.goto(directoryPath)
  await page.getByRole('button', { name: 'Edit name for Engineering', exact: true }).click()
  await page.getByLabel('Department name').fill('Product engineering')
  await page.route('**/api/departments/options', (route) => route.fulfill({ status: 503, json: { detail: 'Unavailable' } }))
  // When
  await page.getByLabel('Department name').press('Enter')
  // Then
  const local = page.getByRole('region', { name: 'Local departments', exact: true })
  await expect(local.getByRole('status')).toContainText('Unable to load local departments')
  await expect(local.getByRole('heading', { name: 'Product engineering', exact: true })).toBeVisible()
  await expect(local.getByRole('heading', { name: siblingDepartment.name, exact: true })).toBeVisible()
  await expect(local.getByRole('button', { name: 'Edit name for Product engineering', exact: true })).toBeFocused()
  await expect(local.getByRole('button', { name: 'Retry', exact: true })).toBeEnabled()
})

for (const cancel of ['Escape', 'Cancel']) {
  test(`${cancel} discards a draft and restores edit focus without a request`, async ({ page }) => {
    // Given
    const state = await prepareLocalDepartments(page, 'org_admin')
    await page.goto(directoryPath)
    const edit = page.getByRole('button', { name: 'Edit name for Engineering', exact: true })
    await edit.click()
    await page.getByLabel('Department name').fill('Discard this draft')
    // When
    if (cancel === 'Escape') await page.getByLabel('Department name').press('Escape')
    else await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    // Then
    await expect(edit).toBeFocused()
    await edit.click()
    await expect(page.getByLabel('Department name')).toHaveValue('Engineering')
    expect(state.patches).toEqual([])
  })
}

for (const status of [409, 500]) {
  test(`rename ${status} stays inline with the draft intact`, async ({ page }) => {
    // Given
    await prepareLocalDepartments(page, 'org_admin')
    await page.route(`**/api/departments/${localDepartment.id}`, (route) => route.fulfill({
      status, json: { detail: status === 409 ? 'Duplicate name' : 'Internal failure' },
    }))
    await page.goto(directoryPath)
    await page.getByRole('button', { name: 'Edit name for Engineering', exact: true }).click()
    const input = page.getByLabel('Department name')
    await input.fill('Operations')
    // When
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    // Then
    await expect(page.getByRole('status')).toContainText(status === 409 ? 'already exists' : 'Unable to save')
    await expect(input).toHaveValue('Operations')
    await expect(input).toHaveAttribute('aria-invalid', String(status === 409))
    await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled()
  })
}

for (const name of ['   ', 'x'.repeat(101)]) {
  test(`invalid ${name.length === 3 ? 'blank' : 'long'} name is rejected before transport`, async ({ page }) => {
    // Given
    const state = await prepareLocalDepartments(page, 'org_admin')
    await page.goto(directoryPath)
    await page.getByRole('button', { name: 'Edit name for Engineering', exact: true }).click()
    const input = page.getByLabel('Department name')
    await input.fill(name)
    // When
    await input.press('Enter')
    // Then
    await expect(input).toHaveAttribute('aria-invalid', 'true')
    await expect(input).toBeFocused()
    await expect(page.getByRole('status')).toContainText(name.length === 3 ? 'Enter a department name' : '100 characters')
    expect(state.patches).toEqual([])
  })
}

test('pending rename prevents duplicate submissions until the response arrives', async ({ page }) => {
  // Given
  await prepareLocalDepartments(page, 'org_admin')
  const release = Promise.withResolvers<void>()
  await page.route(`**/api/departments/${localDepartment.id}`, async (route) => {
    await release.promise
    await route.fulfill({ json: {
      ...localDepartment, name: 'Engineering', tenant_id: originalCompany.id, created_at: '2026-10-18T00:00:00Z',
    } })
  })
  await page.goto(directoryPath)
  await page.getByRole('button', { name: 'Edit name for Engineering', exact: true }).click()
  // When
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Saving...', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled()
  await expect(page.getByLabel('Department name')).toHaveAttribute('readonly', '')
  release.resolve()
  await expect(page.getByRole('button', { name: 'Edit name for Engineering', exact: true })).toBeFocused()
})

test('failed local department requests can retry to empty after loading', async ({ page }) => {
  // Given
  const state = await prepareLocalDepartments(page)
  state.departments = []
  const release = Promise.withResolvers<void>()
  await page.route('**/api/departments/options', async (route) => {
    await release.promise
    await route.fulfill({ status: 503, json: { detail: 'Unavailable' } })
  })
  await page.goto(directoryPath)
  const local = page.getByRole('region', { name: 'Local departments', exact: true })
  await expect(local.getByRole('status')).toHaveText('Loading local departments...')
  release.resolve()
  await expect(local.getByRole('status')).toContainText('Unable to load local departments')
  await page.unroute('**/api/departments/options')
  // When
  await local.getByRole('button', { name: 'Retry', exact: true }).click()
  // Then
  await expect(local).toContainText('No local departments yet. Ask an org admin')
  await expect(local.getByRole('button')).toHaveCount(0)
})
