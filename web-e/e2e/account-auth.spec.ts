import { expect, test } from '@playwright/test'

import { member, prepareAuth } from './account-auth-fixture'

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
    await page.getByLabel('Confirm password', { exact: true }).fill('a-secure-password')
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
  const confirmPassword = page.getByLabel('Confirm password', { exact: true })
  await expect(confirmPassword).toHaveAttribute('type', 'password')
  await expect(confirmPassword).toHaveAttribute('autocomplete', 'new-password')
  await confirmPassword.fill('a-secure-password')
  // When
  await page.getByRole('button', { name: 'Create account', exact: true }).click()
  // Then
  await expect(page).toHaveURL('/app/settings')
  expect(registrations).toEqual([{ email: 'member@example.test', password: 'a-secure-password' }])
})

for (const { confirmation, message } of [
  { confirmation: '', message: 'Confirm your password' },
  { confirmation: 'different-password', message: 'Passwords do not match' },
]) {
  test(`registration rejects ${confirmation ? 'mismatched' : 'missing'} password confirmation without posting`, async ({ page }) => {
    // Given
    const { registrations, errors } = await prepareAuth(page)
    await page.goto('/register')
    await page.getByLabel('Email', { exact: true }).fill('member@example.test')
    await page.getByLabel('Password', { exact: true }).fill('a-secure-password')
    const confirmPassword = page.getByLabel('Confirm password', { exact: true })
    if (confirmation) await confirmPassword.fill(confirmation)
    // When
    await page.getByRole('button', { name: 'Create account', exact: true }).click()
    // Then
    await expect(page.getByRole('alert')).toHaveText(message)
    await expect(confirmPassword).toHaveAttribute('aria-invalid', 'true')
    await expect(confirmPassword).toHaveAccessibleDescription(message)
    await expect(page).toHaveURL('/register')
    expect(registrations).toEqual([])
    expect(errors).toEqual([])
  })
}
