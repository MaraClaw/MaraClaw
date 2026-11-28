import { expect, test, type Page, type Route } from '@playwright/test'

const linkToken = 'reset-link-token-0123456789'
const newerToken = 'newer-reset-link-token-9876543210'
const newPassword = 'a-new-secure-password'

type Reply = (route: Route) => Promise<void>

const rejectLink: Reply = (route) => route.fulfill({ status: 400, json: { detail: 'Invalid or expired reset token' } })
const acceptReset: Reply = (route) => route.fulfill({ json: { ok: true } })

async function prepareReset(page: Page, replies: Reply[]) {
  const submissions: { body: unknown; authorization: string | null }[] = []
  const unexpected: string[] = []
  page.on('pageerror', (error) => unexpected.push(error.message))
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    if (pathname === '/api/auth/providers' && request.method() === 'GET') {
      await route.fulfill({ json: [] })
      return
    }
    const reply = replies[submissions.length]
    if (pathname !== '/api/auth/reset-password' || request.method() !== 'POST' || !reply) {
      unexpected.push(`${request.method()} ${pathname}`)
      await route.abort()
      return
    }
    submissions.push({ body: request.postDataJSON(), authorization: request.headers()['authorization'] ?? null })
    await reply(route)
  })
  return { submissions, unexpected }
}

async function fillNewPassword(page: Page) {
  const password = page.getByLabel('New password', { exact: true })
  const confirmation = page.getByLabel('Confirm new password', { exact: true })
  await password.fill(newPassword)
  await confirmation.fill(newPassword)
  return { password, confirmation }
}

test('a rejected reset link replaces the form with a request for a new link', async ({ page }) => {
  // Given
  const { submissions, unexpected } = await prepareReset(page, [rejectLink])
  await page.goto(`/reset-password?token=${linkToken}`)
  const { confirmation } = await fillNewPassword(page)
  // When
  await confirmation.press('Enter')
  // Then
  const requestLink = page.getByRole('link', { name: 'Request a new link', exact: true })
  await expect(requestLink).toBeFocused()
  await expect(requestLink).toHaveAccessibleDescription(/\S/)
  await expect(page.getByLabel('New password', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Update password', exact: true })).toHaveCount(0)
  expect(submissions).toEqual([{ body: { token: linkToken, new_password: newPassword }, authorization: null }])
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL('/forgot-password')
  expect(unexpected).toEqual([])
})

for (const { failure, reply } of [
  { failure: 'server error', reply: (route: Route) => route.fulfill({ status: 503, body: '' }) },
  { failure: 'network failure', reply: (route: Route) => route.abort('failed') },
  { failure: 'validation error', reply: (route: Route) => route.fulfill({ status: 422, json: { detail: [{ msg: 'Password is too weak' }] } }) },
]) {
  test(`a ${failure} keeps the typed passwords for a retry that signs in`, async ({ page }) => {
    // Given
    const { submissions, unexpected } = await prepareReset(page, [reply, acceptReset])
    await page.goto(`/reset-password?token=${linkToken}`)
    const { password, confirmation } = await fillNewPassword(page)
    const submit = page.getByRole('button', { name: 'Update password', exact: true })
    await submit.click()
    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toBeVisible()
    await expect(password).toHaveValue(newPassword)
    await expect(confirmation).toHaveValue(newPassword)
    await expect(page.getByRole('link', { name: 'Request a new link', exact: true })).toHaveCount(0)
    // When
    await submit.click()
    // Then
    await expect(page).toHaveURL('/login')
    const body = { token: linkToken, new_password: newPassword }
    expect(submissions.map((submission) => submission.body)).toEqual([body, body])
    expect(unexpected).toEqual([])
  })
}

test('a newer link opened after a rejection gets its own reset form', async ({ page }) => {
  // Given
  const { submissions, unexpected } = await prepareReset(page, [rejectLink, acceptReset])
  await page.goto(`/reset-password?token=${linkToken}`)
  const { confirmation } = await fillNewPassword(page)
  await confirmation.press('Enter')
  await expect(page.getByRole('link', { name: 'Request a new link', exact: true })).toBeFocused()
  // When
  await page.evaluate((token) => {
    history.pushState(null, '', `/reset-password?token=${token}`)
    dispatchEvent(new PopStateEvent('popstate'))
  }, newerToken)
  await fillNewPassword(page)
  await page.getByRole('button', { name: 'Update password', exact: true }).click()
  // Then
  await expect(page).toHaveURL('/login')
  expect(submissions.map((submission) => submission.body)).toEqual([
    { token: linkToken, new_password: newPassword },
    { token: newerToken, new_password: newPassword },
  ])
  expect(unexpected).toEqual([])
})

for (const search of ['', '?token=too-short']) {
  test(`an incomplete link ${JSON.stringify(search)} offers a new link without moving focus`, async ({ page }) => {
    // Given
    const { submissions, unexpected } = await prepareReset(page, [])
    // When
    await page.goto(`/reset-password${search}`)
    // Then
    await expect(page.getByRole('link', { name: 'Request a new link', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true)
    await expect(page.getByLabel('New password', { exact: true })).toHaveCount(0)
    expect(submissions).toEqual([])
    expect(unexpected).toEqual([])
  })
}
