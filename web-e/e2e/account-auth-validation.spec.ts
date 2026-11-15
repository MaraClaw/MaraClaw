import { expect, test } from '@playwright/test'

import { prepareAuth } from './account-auth-fixture'

const duplicateMessage = 'That email is already registered.'

for (const scenario of [
  { name: 'duplicate response after an edit', outcome: 'duplicate', edit: true, returnToOriginal: false, laterCheck: false, latestDuplicate: false },
  { name: 'duplicate response after A-B-A edits', outcome: 'duplicate', edit: true, returnToOriginal: true, laterCheck: false, latestDuplicate: false },
  { name: 'available response after a newer duplicate check', outcome: 'available', edit: false, returnToOriginal: false, laterCheck: true, latestDuplicate: true },
  { name: 'duplicate response after a newer available check', outcome: 'duplicate', edit: false, returnToOriginal: false, laterCheck: true, latestDuplicate: false },
  { name: 'rejection after A-B-A edits', outcome: 'rejection', edit: true, returnToOriginal: true, laterCheck: false, latestDuplicate: false },
  { name: 'rejection after a later blur check', outcome: 'rejection', edit: false, returnToOriginal: false, laterCheck: true, latestDuplicate: true },
] as const) {
  test(`registration discards an old ${scenario.name}`, async ({ page }) => {
    // Given: the first request is held while edits or a newer check invalidate it.
    const started = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    let checks = 0
    let currentRejection = false
    const { errors } = await prepareAuth(page, { checkDuplicate: async (route) => {
      checks += 1
      if (currentRejection) {
        await route.fulfill({ status: 400, json: { detail: 'The current email check failed.' } })
        return
      }
      if (checks === 1) {
        started.resolve()
        await release.promise
        if (scenario.outcome === 'rejection') {
          await route.fulfill({ status: 400, json: { detail: 'The earlier email check failed.' } })
          return
        }
        await route.fulfill({ json: { email_exists: scenario.outcome === 'duplicate', has_conflict: false } })
        return
      }
      await route.fulfill({ json: { email_exists: scenario.latestDuplicate, has_conflict: false } })
    } })
    await page.goto('/register')
    const email = page.getByLabel('Email', { exact: true })
    const warning = page.getByText(duplicateMessage, { exact: true })
    await email.fill('first@example.test')
    await email.press('Tab')
    await started.promise
    try {
      if (scenario.edit) {
        await email.fill('second@example.test')
        if (scenario.returnToOriginal) await email.fill('first@example.test')
      }
      if (scenario.laterCheck) {
        const newest = page.waitForResponse('**/api/auth/check-duplicate?*')
        await email.focus()
        await email.press('Tab')
        await (await newest).finished()
        await expect(warning).toHaveCount(scenario.latestDuplicate ? 1 : 0)
      }
      // When: the invalidated request settles, including its rejection path.
      const older = page.waitForResponse('**/api/auth/check-duplicate?*')
      release.resolve()
      await (await older).finished()
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
      if (scenario.outcome === 'rejection') {
        currentRejection = true
        await email.focus()
        await email.press('Tab')
        await expect(page.getByText('The current email check failed.', { exact: true })).toBeVisible()
      }
      // Then: only the newest check can change feedback or raise a toast.
      await expect(warning).toHaveCount(scenario.latestDuplicate ? 1 : 0)
      expect(await page.getByText('The earlier email check failed.', { exact: true }).count()).toBe(0)
      expect(errors).toEqual([])
    } finally {
      release.resolve()
    }
  })
}

test('editing the checked email clears its duplicate warning immediately', async ({ page }) => {
  // Given
  await prepareAuth(page, { checkDuplicate: async (route) => {
    await route.fulfill({ json: { email_exists: true, has_conflict: false } })
  } })
  await page.goto('/register')
  const email = page.getByLabel('Email', { exact: true })
  await email.fill('taken@example.test')
  await email.press('Tab')
  await expect(page.getByText(duplicateMessage, { exact: true })).toBeVisible()
  // When
  await email.fill('available@example.test')
  // Then: no blur is needed to clear the old value's feedback.
  await expect(page.getByText(duplicateMessage, { exact: true })).toHaveCount(0)
  await expect(email).not.toHaveAttribute('aria-invalid', 'true')
  await expect(email).toHaveAccessibleDescription('')
  await email.press('Tab')
  await expect(email).toHaveAttribute('aria-invalid', 'true')
  await expect(email).toHaveAccessibleDescription(duplicateMessage)
})

test('login fields describe validation errors and clear invalid state after correction', async ({ page }) => {
  // Given
  const { logins } = await prepareAuth(page)
  await page.goto('/login')
  const email = page.getByLabel('Email', { exact: true })
  const password = page.getByLabel('Password', { exact: true })
  // When
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  // Then
  await expect(email).toHaveAttribute('aria-invalid', 'true')
  await expect(email).toHaveAccessibleDescription('Enter your email')
  await expect(password).toHaveAttribute('aria-invalid', 'true')
  await expect(password).toHaveAccessibleDescription('Enter your password')
  expect(logins).toEqual([])
  await email.fill('member@example.test')
  await password.fill('a-secure-password')
  for (const field of [email, password]) {
    await expect(field).not.toHaveAttribute('aria-invalid', 'true')
    await expect(field).toHaveAccessibleDescription('')
  }
  await expect(email).toHaveAttribute('autocomplete', 'username')
  await expect(password).toHaveAttribute('autocomplete', 'current-password')
})

test('registration fields describe email, password and invitation validation errors', async ({ page }) => {
  // Given
  const { registrations } = await prepareAuth(page)
  await page.goto('/register')
  const email = page.getByLabel('Email', { exact: true })
  const password = page.getByLabel('Password', { exact: true })
  const invitation = page.getByLabel('Invitation code', { exact: false })
  await email.fill('member@')
  await password.fill('123')
  await page.getByLabel('Confirm password', { exact: true }).fill('123')
  await invitation.fill('x'.repeat(129))
  // When
  await page.getByRole('button', { name: 'Create account', exact: true }).click()
  // Then
  for (const [field, message] of [
    [email, 'Enter a valid email'], [password, 'Use at least 6 characters'], [invitation, 'Invitation code is too long'],
  ] as const) {
    await expect(field).toHaveAttribute('aria-invalid', 'true')
    await expect(field).toHaveAccessibleDescription(message)
  }
  expect(registrations).toEqual([])
  await email.fill('member@example.test')
  await password.fill('a-secure-password')
  await invitation.fill('company-invite')
  for (const field of [email, password, invitation]) {
    await expect(field).not.toHaveAttribute('aria-invalid', 'true')
    await expect(field).toHaveAccessibleDescription('')
  }
  await expect(email).toHaveAttribute('autocomplete', 'email')
  await expect(password).toHaveAttribute('autocomplete', 'new-password')
  await expect(invitation).toHaveAttribute('autocomplete', 'off')
})

for (const code of ['', '   ']) {
  test(`required invitation rejects ${code ? 'whitespace' : 'empty'} before allowing a corrected submission`, async ({ page }) => {
    // Given
    const { registrations, errors } = await prepareAuth(page, { invitationRequired: true })
    await page.goto('/register')
    const invitation = page.getByLabel('Invitation code', { exact: true })
    await expect(invitation).toBeVisible()
    await page.getByLabel('Email', { exact: true }).fill('member@example.test')
    await page.getByLabel('Password', { exact: true }).fill('a-secure-password')
    await page.getByLabel('Confirm password', { exact: true }).fill('a-secure-password')
    await invitation.fill(code)
    // When
    await page.getByRole('button', { name: 'Create account', exact: true }).click()
    // Then
    await expect(invitation).toHaveAttribute('aria-invalid', 'true')
    await expect(invitation).toHaveAccessibleDescription('An invitation code is required')
    expect(registrations).toEqual([])
    await invitation.fill('  company-invite  ')
    await page.getByRole('button', { name: 'Create account', exact: true }).click()
    await expect(page).toHaveURL('/app/settings')
    expect(registrations).toEqual([{ email: 'member@example.test', password: 'a-secure-password', invitation_code: 'company-invite' }])
    expect(errors).toEqual([])
  })
}
