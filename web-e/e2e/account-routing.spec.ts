import { expect, test, type Page } from '@playwright/test'

import type { UserOut } from '../src/lib/types/auth'

const tokenKey = 'maraclaw-enduser-token'
const syntheticToken = 'account-routing-synthetic-session'
const company = {
  id: 'f0c4b91d-7d55-4d66-845c-320f76e938de', name: 'Routing Company', slug: 'routing',
  im_provider: 'none', is_active: true,
}
const member: UserOut = {
  id: 'f936188e-a374-4edf-9b05-ac6c9cb236a6', username: 'routing-member', email: 'member@routing.test',
  display_name: 'Routing member', role: 'member', tenant_id: company.id, email_verified: true,
  is_platform_admin: false,
}

async function prepareMember(page: Page, mustChangePassword: boolean) {
  const onboardingRequests: string[] = []
  const passwordRequired = { status: 403, json: { detail: 'Password change required' } }
  await page.addInitScript(({ tokenKey, syntheticToken }) => {
    localStorage.setItem(tokenKey, syntheticToken)
  }, { tokenKey, syntheticToken })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    switch (path) {
      case '/api/auth/me':
        await route.fulfill({ json: { ...member, must_change_password: mustChangePassword } })
        break
      case '/api/auth/providers':
      case '/api/auth/my-tenants':
        await route.fulfill({ json: [] })
        break
      case '/api/tenants/me':
        await route.fulfill(mustChangePassword ? passwordRequired : { json: company })
        break
      case '/api/notifications/unread-count':
        await route.fulfill(mustChangePassword ? passwordRequired : { json: { unread_count: 0 } })
        break
      case '/api/agents/':
        onboardingRequests.push(path)
        await route.fulfill(mustChangePassword ? passwordRequired : { json: [] })
        break
      case '/api/onboarding/status':
        onboardingRequests.push(path)
        await route.fulfill(mustChangePassword ? passwordRequired : {
          json: { exists: false, status: 'not_started', current_step: 'welcome' },
        })
        break
      default:
        throw new Error(`Unexpected API request: ${route.request().method()} ${path}`)
    }
  })
  return onboardingRequests
}

for (const mustChangePassword of [true, false]) {
  const condition = mustChangePassword ? 'a password change is required' : 'onboarding is incomplete'

  test(`account renders when ${condition}`, async ({ page }) => {
    // Given
    const onboardingRequests = await prepareMember(page, mustChangePassword)
    // When
    await page.goto('/app/account')
    // Then
    await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible()
    await expect(page.getByLabel('Display name')).toHaveValue(member.display_name)
    await expect(page.getByLabel('Current password', { exact: true })).toBeEnabled()
    await expect(page).toHaveURL('/app/account')
    expect(onboardingRequests).toEqual([])
  })

  test(`settings renders when ${condition}`, async ({ page }) => {
    // Given
    const onboardingRequests = await prepareMember(page, mustChangePassword)
    // When
    await page.goto('/app/settings')
    // Then
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
    await expect(page.getByRole('main').getByRole('button', { name: 'Sign out', exact: true })).toBeEnabled()
    await expect(page).toHaveURL('/app/settings')
    expect(onboardingRequests).toEqual([])
  })
}

for (const path of ['/app', '/app/agents', '/app/onboarding', '/app/agents/restricted-agent/settings']) {
  test(`${path} redirects to account when a password change is required`, async ({ page }) => {
    // Given
    const onboardingRequests = await prepareMember(page, true)
    // When
    await page.goto(path)
    // Then
    await expect(page).toHaveURL('/app/account')
    await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible()
    await expect(page.getByLabel('Current password', { exact: true })).toBeEnabled()
    expect(onboardingRequests).toEqual([])
  })
}

for (const path of ['/app', '/app/agents']) {
  test(`${path} opens onboarding for an ordinary first-run member`, async ({ page }) => {
    // Given
    const onboardingRequests = await prepareMember(page, false)
    // When
    await page.goto(path)
    // Then
    await expect(page).toHaveURL('/app/onboarding')
    await expect(page.getByRole('heading', { name: 'Meet your assistant', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Create assistant', exact: true })).toBeEnabled()
    expect(onboardingRequests).toEqual(expect.arrayContaining(['/api/agents/', '/api/onboarding/status']))
  })
}
