import { expect, test, type Page } from '@playwright/test'

import type { UserOut } from '../src/lib/types/auth'

const member: UserOut = {
  id: 'f936188e-a374-4edf-9b05-ac6c9cb236a6', email: 'member@example.test',
  display_name: 'Member', role: 'member', tenant_id: 'company', email_verified: true,
  must_change_password: true,
}

async function prepareAuth(page: Page) {
  const logins: unknown[] = []
  const registrations: unknown[] = []
  const availability: string[][][] = []
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    switch (url.pathname) {
      case '/api/auth/providers':
      case '/api/auth/my-tenants':
        await route.fulfill({ json: [] })
        break
      case '/api/auth/registration-config':
        await route.fulfill({ json: { invitation_code_required: false } })
        break
      case '/api/auth/check-duplicate':
        availability.push([...url.searchParams])
        await route.fulfill({ json: { email_exists: false, has_conflict: false } })
        break
      case '/api/auth/register/init':
        registrations.push(request.postDataJSON())
        await route.fulfill({ json: {
          access_token: 'registered-account', user: member,
          identity: { id: 'identity', email: member.email },
        } })
        break
      case '/api/auth/login':
        logins.push(request.postDataJSON())
        await route.fulfill({ json: {
          access_token: 'email-account', user: member, identity: { id: 'identity', email: member.email },
        } })
        break
      case '/api/auth/me':
        await route.fulfill({ json: member })
        break
      case '/api/notifications/unread-count':
        await route.fulfill({ json: { unread_count: 0 } })
        break
      case '/api/tenants/me':
        await route.fulfill({ json: { id: 'company', name: 'Company', slug: 'company' } })
        break
      default:
        throw new Error(`Unexpected auth request: ${request.method()} ${url.pathname}`)
    }
  })
  return { logins, registrations, availability, errors }
}

for (const identifier of ['retired-member', '+15551234567', 'member@', '']) {
  test(`login rejects ${JSON.stringify(identifier)} without submitting credentials`, async ({ page }) => {
    // Given
    const { logins } = await prepareAuth(page)
    await page.goto('/login')
    const email = page.getByRole('textbox', { name: 'Email', exact: true })
    await email.fill(identifier)
    await page.getByLabel('Password', { exact: true }).fill('a-secure-password')
    // When
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    // Then
    await expect(email).toBeFocused()
    expect(logins).toEqual([])
    await expect(page).toHaveURL('/login')
  })
}

test('email and password login reaches the existing password-change gate', async ({ page }) => {
  // Given
  const { logins, errors } = await prepareAuth(page)
  await page.goto('/login')
  const email = page.getByRole('textbox', { name: 'Email', exact: true })
  await expect(email).toHaveAttribute('type', 'email')
  await email.fill('member@example.test')
  await page.getByLabel('Password', { exact: true }).fill('a-secure-password')
  // When
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  // Then
  await expect(page).toHaveURL('/app/settings')
  await expect(page.getByLabel('Display name')).toHaveValue(member.display_name)
  await expect(page.getByLabel('Username', { exact: true })).toHaveCount(0)
  expect(logins).toEqual([{ login_identifier: 'member@example.test', password: 'a-secure-password' }])
  expect(errors).toEqual([])
})

for (const email of ['shared@one.example', 'shared@two.example']) {
  test(`registration sends no generated username for ${email}`, async ({ page }) => {
    // Given
    const { registrations, availability, errors } = await prepareAuth(page)
    await page.goto('/register')
    await expect(page.getByLabel('Username', { exact: true })).toHaveCount(0)
    await page.getByLabel('Email', { exact: true }).fill(email)
    await page.getByLabel('Password', { exact: true }).fill('a-secure-password')
    await page.getByLabel('Invitation code', { exact: false }).fill('  company-invite  ')
    // When
    await page.getByRole('button', { name: 'Create account', exact: true }).click()
    // Then
    await expect(page).toHaveURL('/app/settings')
    expect(registrations).toEqual([{ email, password: 'a-secure-password', invitation_code: 'company-invite' }])
    expect(availability).toEqual([[['email', email]]])
    expect(errors).toEqual([])
  })
}

test('registration omits optional display name and invitation code', async ({ page }) => {
  // Given
  const { registrations } = await prepareAuth(page)
  await page.goto('/register')
  await page.getByLabel('Email', { exact: true }).fill('member@example.test')
  await page.getByLabel('Password', { exact: true }).fill('a-secure-password')
  // When
  await page.getByRole('button', { name: 'Create account', exact: true }).click()
  // Then
  await expect(page).toHaveURL('/app/settings')
  expect(registrations).toEqual([{ email: 'member@example.test', password: 'a-secure-password' }])
})
