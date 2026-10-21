import { expect, test, type Page } from '@playwright/test'

import type { UserOut } from '../src/lib/types/auth'

const tokenKey = 'maraclaw-enduser-token'
const syntheticToken = 'account-routing-synthetic-session'
const company = {
  id: 'f0c4b91d-7d55-4d66-845c-320f76e938de', name: 'Routing Company', slug: 'routing',
  im_provider: 'none', is_active: true,
}
const member: UserOut = {
  id: 'f936188e-a374-4edf-9b05-ac6c9cb236a6', email: 'member@routing.test',
  display_name: 'Routing member', role: 'member', tenant_id: company.id, email_verified: true,
  is_platform_admin: false,
}

async function prepareMember(page: Page, mustChangePassword: boolean) {
  const onboardingRequests: string[] = []
  const passwordRequests: unknown[] = []
  const identityRequests: string[] = []
  let requiresPassword = mustChangePassword
  const passwordRequired = { status: 403, json: { detail: 'Password change required' } }
  await page.addInitScript(({ tokenKey, syntheticToken }) => {
    localStorage.setItem(tokenKey, syntheticToken)
  }, { tokenKey, syntheticToken })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    switch (path) {
      case '/api/auth/me':
        identityRequests.push(route.request().method())
        await route.fulfill({ json: { ...member, must_change_password: requiresPassword } })
        break
      case '/api/auth/me/password':
        expect(route.request().method()).toBe('PUT')
        passwordRequests.push(route.request().postDataJSON())
        requiresPassword = false
        await route.fulfill({ json: { ok: true, must_change_password: false } })
        break
      case '/api/auth/providers':
      case '/api/auth/my-tenants':
        await route.fulfill({ json: [] })
        break
      case '/api/tenants/me':
        await route.fulfill(requiresPassword ? passwordRequired : { json: company })
        break
      case '/api/notifications/unread-count':
        await route.fulfill(requiresPassword ? passwordRequired : { json: { unread_count: 0 } })
        break
      case '/api/agents/':
        onboardingRequests.push(path)
        await route.fulfill(requiresPassword ? passwordRequired : { json: [] })
        break
      case '/api/onboarding/status':
        onboardingRequests.push(path)
        await route.fulfill(requiresPassword ? passwordRequired : {
          json: { exists: false, status: 'not_started', current_step: 'welcome' },
        })
        break
      default:
        throw new Error(`Unexpected API request: ${route.request().method()} ${path}`)
    }
  })
  return { onboardingRequests, passwordRequests, identityRequests }
}

for (const mustChangePassword of [true, false]) {
  const condition = mustChangePassword ? 'a password change is required' : 'onboarding is incomplete'

  test(`legacy account links open settings when ${condition}`, async ({ page }) => {
    // Given
    const { onboardingRequests } = await prepareMember(page, mustChangePassword)
    // When
    await page.goto('/app/account')
    // Then
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
    await expect(page.getByLabel('Display name')).toHaveValue(member.display_name)
    await expect(page.getByLabel('Current password', { exact: true })).toBeEnabled()
    await expect(page).toHaveURL('/app/settings')
    expect(onboardingRequests).toEqual([])
  })

  test(`settings renders when ${condition}`, async ({ page }) => {
    // Given
    const { onboardingRequests } = await prepareMember(page, mustChangePassword)
    // When
    await page.goto('/app/settings')
    // Then
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
    await expect(page.getByRole('main').locator('h1')).toHaveCount(1)
    await expect(page.getByLabel('Username', { exact: true })).toHaveCount(0)
    await expect(page.locator('main [data-slot="card-title"]')).toHaveText(['Profile', 'Password', 'Organization', 'Session'])
    await expect(page.locator('nav a[href="/app/account"]')).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'Account', exact: true, includeHidden: true })).toHaveCount(0)
    await expect(page.getByRole('main').getByRole('button', { name: 'Sign out', exact: true })).toBeEnabled()
    await expect(page).toHaveURL('/app/settings')
    expect(onboardingRequests).toEqual([])
  })
}

for (const path of ['/app', '/app/agents', '/app/onboarding', '/app/agents/restricted-agent/settings']) {
  test(`${path} redirects to settings when a password change is required`, async ({ page }) => {
    // Given
    const { onboardingRequests } = await prepareMember(page, true)
    // When
    await page.goto(path)
    // Then
    await expect(page).toHaveURL('/app/settings')
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
    await expect(page.getByLabel('Current password', { exact: true })).toBeEnabled()
    expect(onboardingRequests).toEqual([])
  })
}

for (const path of ['/app', '/app/agents']) {
  test(`${path} opens onboarding for an ordinary first-run member`, async ({ page }) => {
    // Given
    const { onboardingRequests } = await prepareMember(page, false)
    // When
    await page.goto(path)
    // Then
    await expect(page).toHaveURL('/app/onboarding')
    await expect(page.getByRole('heading', { name: 'Meet your assistant', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Create assistant', exact: true })).toBeEnabled()
    expect(onboardingRequests).toEqual(expect.arrayContaining(['/api/agents/', '/api/onboarding/status']))
  })
}

test('settings saves the trimmed profile and refreshes the signed-in identity', async ({ page }) => {
  // Given
  await prepareMember(page, false)
  const updated = { ...member, display_name: 'Updated member' }
  const updates: unknown[] = []
  let refreshes = 0
  await page.route('**/api/auth/me', async (route) => {
    if (route.request().method() === 'PATCH') {
      updates.push(route.request().postDataJSON())
      await route.fulfill({ json: member })
    } else {
      refreshes += 1
      await route.fulfill({ json: updates.length > 0 ? updated : member })
    }
  })
  await page.goto('/app/settings')
  await page.getByLabel('Display name').fill('  Updated member  ')
  // When
  await page.getByRole('button', { name: 'Save profile', exact: true }).click()
  // Then
  await expect(page.getByText(`Signed in as ${updated.display_name}.`, { exact: false })).toBeVisible()
  await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(1)
  expect(updates).toEqual([{ display_name: 'Updated member' }])
  expect(refreshes).toBe(2)
})

for (const invalid of [
  { name: 'empty display name', field: 'Display name', value: '   ' },
  { name: 'long display name', field: 'Display name', value: 'x'.repeat(101) },
]) {
  test(`settings rejects an ${invalid.name} before updating the profile`, async ({ page }) => {
    // Given
    const { identityRequests } = await prepareMember(page, false)
    await page.goto('/app/settings')
    await page.getByLabel(invalid.field, { exact: true }).fill(invalid.value)
    // When
    await page.getByRole('button', { name: 'Save profile', exact: true }).click()
    // Then
    await expect(page.getByLabel(invalid.field, { exact: true })).toBeFocused()
    expect(identityRequests).toEqual(['GET'])
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
  })
}

test('settings updates the password, refreshes identity and releases the forced-password gate', async ({ page }) => {
  // Given
  const { onboardingRequests, passwordRequests, identityRequests } = await prepareMember(page, true)
  await page.goto('/app/settings')
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByLabel('Current password', { exact: true }).fill('current-password')
  await page.getByLabel('New password', { exact: true }).fill('replacement-password')
  await page.getByLabel('Confirm new password', { exact: true }).fill('replacement-password')
  // When
  await page.getByRole('button', { name: 'Update password', exact: true }).click()
  // Then
  await expect(page.getByRole('alert')).toHaveCount(0)
  await expect(page.getByLabel('Current password', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('New password', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('Confirm new password', { exact: true })).toHaveValue('')
  await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(1)
  expect(passwordRequests).toEqual([{ old_password: 'current-password', new_password: 'replacement-password' }])
  expect(identityRequests).toEqual(['GET', 'GET'])
  expect(onboardingRequests).toEqual([])
  await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
  await expect(page).toHaveURL('/app/onboarding')
  await expect(page.getByRole('button', { name: 'Create assistant', exact: true })).toBeEnabled()
})

for (const invalid of [
  { name: 'missing current password', old: '', next: 'replacement', confirm: 'replacement', field: 'Current password' },
  { name: 'short password', old: 'current', next: 'short', confirm: 'short', field: 'New password' },
  { name: 'mismatched confirmation', old: 'current', next: 'replacement', confirm: 'different', field: 'Confirm new password' },
  { name: 'unchanged password', old: 'current', next: 'current', confirm: 'current', field: 'New password' },
]) {
  test(`settings rejects a ${invalid.name} before changing the password`, async ({ page }) => {
    // Given
    const { passwordRequests, identityRequests } = await prepareMember(page, false)
    await page.goto('/app/settings')
    await page.getByLabel('Current password', { exact: true }).fill(invalid.old)
    await page.getByLabel('New password', { exact: true }).fill(invalid.next)
    await page.getByLabel('Confirm new password', { exact: true }).fill(invalid.confirm)
    // When
    await page.getByRole('button', { name: 'Update password', exact: true }).click()
    // Then
    await expect(page.getByLabel(invalid.field, { exact: true })).toHaveAttribute('aria-invalid', 'true')
    expect(passwordRequests).toEqual([])
    expect(identityRequests).toEqual(['GET'])
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
  })
}

test('settings places active connected sign-in after password and retains unlinking', async ({ page }) => {
  // Given
  await prepareMember(page, false)
  await page.route('**/api/auth/providers', (route) => route.fulfill({ json: [
    { id: 'active', provider_type: 'example', name: 'Company sign-in', is_active: true },
    { id: 'inactive', provider_type: 'hidden', name: 'Inactive sign-in', is_active: false },
  ] }))
  const unlinks: unknown[] = []
  await page.route('**/api/auth/example/unbind', async (route) => {
    expect(route.request().method()).toBe('POST')
    unlinks.push(route.request().postDataJSON())
    await route.fulfill({ json: member })
  })
  await page.goto('/app/settings')
  await expect(page.locator('main [data-slot="card-title"]')).toHaveText(['Profile', 'Password', 'Connected sign-in', 'Organization', 'Session'])
  await expect(page.getByRole('button', { name: 'Connect', exact: true })).toHaveCount(1)
  // When
  await page.getByRole('button', { name: 'Remove', exact: true }).click()
  // Then
  await expect(page.locator('[data-sonner-toast][data-type="success"]')).toHaveCount(1)
  expect(unlinks).toEqual([{ provider_type: 'example' }])
})
