import { expect, test, type Page } from '@playwright/test'

import type { UserOut } from '../src/lib/types/auth'

const tokenKey = 'maraclaw-enduser-token'
const originalToken = 'member-before-change'
const replacementToken = 'member-after-change'
const company = {
  id: 'c919ad90-90be-4a1d-b699-1d02138b7f45', name: 'Example Company', slug: 'example',
  im_provider: 'none', is_active: true,
}
const fallback = { ...company, id: 'bdd07e40-83a6-48f3-9049-5ffae4774f75', name: 'OpenClaw', slug: 'openclaw' }
const originalUser: UserOut = {
  id: 'a4ad792f-8901-44f0-a9b6-8947da623f38', email: 'member@example.test',
  display_name: 'Original member', role: 'member', tenant_id: null, email_verified: true,
  must_change_password: false, is_platform_admin: false,
}
const refreshedUser: UserOut = {
  ...originalUser, display_name: 'Refreshed member', tenant_id: company.id,
}

async function prepareMember(page: Page, user: UserOut = originalUser, replacementUser: UserOut = refreshedUser) {
  await page.addInitScript(({ tokenKey, originalToken }) => {
    localStorage.setItem(tokenKey, originalToken)
  }, { tokenKey, originalToken })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    switch (path) {
      case '/api/auth/me':
        await route.fulfill({ json: route.request().headers().authorization === `Bearer ${replacementToken}` ? replacementUser : user })
        break
      case '/api/tenants/lookup-by-email':
        await route.fulfill({ json: { match: company, fallback } })
        break
      case '/api/tenants/me':
        await route.fulfill({ json: company })
        break
      case '/api/onboarding/status':
        await route.fulfill({ json: { status: 'completed' } })
        break
      default:
        await route.fulfill({ json: [] })
    }
  })
}

const flows = [
  { name: 'suggested join', route: '/join', endpoint: 'join-suggested', button: `Join ${company.name}`, tenant: company },
  { name: 'default join', route: '/join', endpoint: 'join-default', button: `Continue with ${fallback.name}`, tenant: fallback },
  { name: 'invite transfer', route: '/transfer', endpoint: 'transfer', button: 'Transfer with invite', tenant: company },
  { name: 'domain transfer', route: '/transfer', endpoint: 'transfer', button: `Transfer to ${company.name}`, tenant: company },
  { name: 'default transfer', route: '/transfer', endpoint: 'transfer', button: `Transfer to ${fallback.name}`, tenant: fallback },
] as const

async function openFlow(page: Page, flow: (typeof flows)[number]) {
  await page.goto(flow.route)
  if (flow.route === '/transfer') {
    await page.getByLabel('Current password').fill('current-password')
    if (flow.name === 'invite transfer') await page.getByLabel('Invitation code').fill('company-invite')
  }
  await expect(page.getByRole('button', { name: flow.button, exact: true })).toBeEnabled()
}

for (const flow of flows) {
  test(`${flow.name} refreshes identity from a userless organization response`, async ({ page }) => {
    // Given
    await prepareMember(
      page,
      { ...originalUser, tenant_id: flow.route === '/transfer' ? (flow.tenant.id === company.id ? fallback.id : company.id) : null },
      { ...refreshedUser, tenant_id: flow.tenant.id },
    )
    await page.route(`**/api/tenants/${flow.endpoint}`, (route) => route.fulfill({
      json: { tenant: flow.tenant, role: 'member', access_token: replacementToken },
    }))
    await openFlow(page, flow)
    const refreshed = page.waitForRequest((request) =>
      new URL(request.url()).pathname === '/api/auth/me' && request.headers().authorization === `Bearer ${replacementToken}`,
    )
    // When
    await page.getByRole('button', { name: flow.button, exact: true }).click()
    // Then
    await refreshed
    await expect(page).toHaveURL(/\/app(?:\/|$)/)
    await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(1)
    await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
    await expect(page.getByText(`Signed in as ${refreshedUser.display_name}.`, { exact: false })).toBeVisible()
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
  })
}

for (const flow of flows.filter((flow) => flow.name !== 'domain transfer' && flow.name !== 'default transfer')) {
  test(`${flow.name} keeps the current session when the organization request fails`, async ({ page }) => {
    // Given
    await prepareMember(page, { ...originalUser, tenant_id: flow.route === '/transfer' ? fallback.id : null })
    await page.route(`**/api/tenants/${flow.endpoint}`, (route) => route.fulfill({
      status: 403, json: { detail: 'Organization change refused' },
    }))
    await openFlow(page, flow)
    // When
    await page.getByRole('button', { name: flow.button, exact: true }).click()
    // Then
    await expect(page.getByRole('alert')).toHaveText('Organization change refused')
    await expect(page).toHaveURL(flow.route)
    await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(0)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
  })
}

for (const response of [
  { name: 'tokenless canonical response', json: { tenant: fallback, role: 'member', access_token: null } },
  { name: 'legacy token session', json: { access_token: replacementToken, user: refreshedUser } },
]) {
  test(`join rejects a ${response.name} without replacing the current session`, async ({ page }) => {
    // Given
    await prepareMember(page)
    await page.route('**/api/tenants/join-default', (route) => route.fulfill({ json: response.json }))
    await openFlow(page, flows[1])
    // When
    await page.getByRole('button', { name: flows[1].button, exact: true }).click()
    // Then
    await expect(page.getByRole('alert')).toBeVisible()
    await expect(page).toHaveURL('/join')
    await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(0)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
  })
}

test('join waits for the replacement-token identity before reporting success', async ({ page }) => {
  // Given
  await prepareMember(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/auth/me', async (route) => {
    if (route.request().headers().authorization === `Bearer ${replacementToken}`) {
      requested.resolve()
      await release.promise
      await route.fulfill({ json: refreshedUser })
    } else await route.fulfill({ json: originalUser })
  })
  await page.route('**/api/tenants/join-default', (route) => route.fulfill({
    json: { tenant: fallback, role: 'member', access_token: replacementToken },
  }))
  await openFlow(page, flows[1])
  // When
  await page.getByRole('button', { name: flows[1].button, exact: true }).click()
  // Then
  await requested.promise
  await expect(page).toHaveURL('/join')
  await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(0)
  release.resolve()
  await expect(page).toHaveURL(/\/app(?:\/|$)/)
})

for (const outcome of ['unauthorized', 'platform-admin'] as const) {
  test(`join does not admit a session when refreshed identity is ${outcome}`, async ({ page }) => {
    // Given
    await prepareMember(page)
    await page.route('**/api/auth/me', (route) => {
      if (route.request().headers().authorization !== `Bearer ${replacementToken}`) return route.fulfill({ json: originalUser })
      return outcome === 'unauthorized'
        ? route.fulfill({ status: 401, json: { detail: 'Expired replacement token' } })
        : route.fulfill({ json: { ...refreshedUser, role: 'platform_admin', is_platform_admin: true } })
    })
    await page.route('**/api/tenants/join-default', (route) => route.fulfill({
      json: { tenant: fallback, role: 'member', access_token: replacementToken },
    }))
    await openFlow(page, flows[1])
    // When
    await page.getByRole('button', { name: flows[1].button, exact: true }).click()
    // Then
    await expect(page).toHaveURL('/login')
    await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(0)
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBeNull()
  })
}

test('join preserves the refreshed identity force-password gate', async ({ page }) => {
  // Given
  await prepareMember(page)
  await page.route('**/api/auth/me', (route) => route.fulfill({
    json: route.request().headers().authorization === `Bearer ${replacementToken}`
      ? { ...refreshedUser, must_change_password: true } : originalUser,
  }))
  await page.route('**/api/tenants/join-default', (route) => route.fulfill({
    json: { tenant: fallback, role: 'member', access_token: replacementToken },
  }))
  await openFlow(page, flows[1])
  // When
  await page.getByRole('button', { name: flows[1].button, exact: true }).click()
  // Then
  await expect(page).toHaveURL('/app/settings')
  await expect(page.getByLabel('Display name')).toHaveValue(refreshedUser.display_name)
  await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
  await expect(page).toHaveURL('/app/settings')
  await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
  await expect(page).toHaveURL('/app/settings')
})
