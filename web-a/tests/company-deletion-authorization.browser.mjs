import assert from 'node:assert/strict'
import { expect } from '@playwright/test'

async function directMount(page, detail = false) {
  await page.route('**/src/main.tsx', (route) => route.fulfill({
    contentType: 'text/javascript', body: 'import "/tests/company-deletion-authorization-fixture.tsx"',
  }))
  await page.goto(new URL(`/companies/alpha${detail ? '?detail' : ''}`, page.url()).href)
  await expect(page.getByTestId('authorization')).toContainText('authenticated:')
}

async function refreshRole(page, state, role) {
  const generation = await page.getByTestId('session-generation').textContent()
  state.role = role
  await page.getByRole('button', { name: 'Refresh authorization' }).click()
  await expect(page.getByTestId('authorization')).toHaveText(`authenticated:${role}`)
  await expect(page.getByTestId('session-generation')).toHaveText(generation)
}

async function assertHidden(page) {
  await expect(page.getByRole('heading', { name: 'Delete company', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Preview deletion|Retry preview|Delete company permanently/ })).toHaveCount(0)
  await expect(page.getByLabel('Enter Alpha Labs to confirm')).toHaveCount(0)
  await expect(page.locator('dl')).toHaveCount(0)
}

export async function companyDeletionAuthorizationScenarios(scenario) {
  for (const detail of [false, true]) await scenario(`directly mounted ${detail ? 'company detail' : 'deletion component'} denies organization admins`, async (page, state) => {
    state.role = 'org_admin'

    await directMount(page, detail)

    if (detail) await expect(page.getByRole('heading', { name: 'Alpha Labs' })).toBeVisible()
    await assertHidden(page)
    await page.getByRole('button', { name: 'Invalidate preview' }).click()
    assert.equal(state.previewCalls, 0)
    assert.equal(state.deleteCalls, 0)
  })

  await scenario('role loss before preview prevents deletion requests without replacing the session', async (page, state) => {
    await directMount(page)
    await expect(page.getByRole('button', { name: 'Preview deletion' })).toBeVisible()

    await refreshRole(page, state, 'org_admin')

    await assertHidden(page)
    await page.getByRole('button', { name: 'Invalidate preview' }).click()
    assert.equal(state.previewCalls, 0)
    assert.equal(state.deleteCalls, 0)
  })

  await scenario('role loss discards a ready preview and exact-name confirmation before reauthorization', async (page, state) => {
    await directMount(page)
    await page.getByRole('button', { name: 'Preview deletion' }).click()
    await page.getByLabel('Enter Alpha Labs to confirm').fill('Alpha Labs')
    await expect(page.getByRole('button', { name: 'Delete company permanently' })).toBeEnabled()

    await refreshRole(page, state, 'org_admin')

    await assertHidden(page)
    await page.getByRole('button', { name: 'Invalidate preview' }).click()
    assert.equal(state.previewCalls, 1)
    assert.equal(state.deleteCalls, 0)
    await refreshRole(page, state, 'platform_admin')
    await expect(page.getByRole('button', { name: 'Preview deletion' })).toBeVisible()
    await expect(page.getByLabel('Enter Alpha Labs to confirm')).toHaveCount(0)
    assert.equal(state.previewCalls, 1)
    await page.getByRole('button', { name: 'Preview deletion' }).click()
    await expect(page.getByLabel('Enter Alpha Labs to confirm')).toHaveValue('')
    await expect(page.getByRole('button', { name: 'Delete company permanently' })).toBeDisabled()
    assert.equal(state.previewCalls, 2)
  })

  await scenario('role loss removes failed-preview retry without sending another preview', async (page, state) => {
    state.previewStatus = 403
    await directMount(page)
    await page.getByRole('button', { name: 'Preview deletion' }).click()
    await expect(page.getByRole('button', { name: 'Retry preview' })).toBeVisible()

    await refreshRole(page, state, 'org_admin')

    await assertHidden(page)
    await page.getByRole('button', { name: 'Invalidate preview' }).click()
    assert.equal(state.previewCalls, 1)
    assert.equal(state.deleteCalls, 0)
  })

  await scenario('role loss prevents an already queued preview from starting its request', async (page, state) => {
    await directMount(page)
    await page.getByRole('button', { name: 'Pause mutations' }).click()
    await page.getByRole('button', { name: 'Preview deletion' }).click()
    await expect(page.getByRole('button', { name: 'Preview deletion' })).toHaveCount(0)
    assert.equal(state.previewCalls, 0)

    await refreshRole(page, state, 'org_admin')
    await page.getByRole('button', { name: 'Resume mutations' }).click()
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))

    await assertHidden(page)
    await expect(page.getByRole('alert')).toHaveCount(0)
    assert.equal(state.previewCalls, 0)
    assert.equal(state.deleteCalls, 0)
  })

  await scenario('role loss prevents an already queued deletion from starting its request', async (page, state) => {
    await directMount(page)
    await page.getByRole('button', { name: 'Preview deletion' }).click()
    await page.getByLabel('Enter Alpha Labs to confirm').fill('Alpha Labs')
    await page.getByRole('button', { name: 'Pause mutations' }).click()
    await page.getByRole('button', { name: 'Delete company permanently' }).click()
    await expect(page.getByRole('button', { name: 'Deleting company' })).toBeDisabled()
    assert.equal(state.deleteCalls, 0)

    await refreshRole(page, state, 'org_admin')
    await page.getByRole('button', { name: 'Resume mutations' }).click()
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))

    await assertHidden(page)
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByText('Company deleted', { exact: true })).toHaveCount(0)
    assert.equal(state.deleteCalls, 0)
  })

  for (const operation of ['preview', 'delete']) for (const status of [200, 403]) await scenario(`role loss fences pending ${operation} ${status} without session replacement`, async (page, state) => {
    await directMount(page)
    const gate = Promise.withResolvers()
    if (operation === 'delete') {
      await page.getByRole('button', { name: 'Preview deletion' }).click()
      await page.getByLabel('Enter Alpha Labs to confirm').fill('Alpha Labs')
      state.deleteGate = gate
      state.deleteStatus = status
      await page.getByRole('button', { name: 'Delete company permanently' }).click()
    } else {
      state.previewGate = gate
      state.previewStatus = status
      await page.getByRole('button', { name: 'Preview deletion' }).click()
    }
    await state[`${operation}Started`].promise

    await refreshRole(page, state, 'org_admin')
    gate.resolve()
    await state[`${operation}Delivered`].promise
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))

    await assertHidden(page)
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByText('Company deleted', { exact: true })).toHaveCount(0)
    assert.equal(new URL(page.url()).pathname, '/companies/alpha')
    assert.equal(state.previewCalls, 1)
    assert.equal(state.deleteCalls, operation === 'delete' ? 1 : 0)
  })
}
