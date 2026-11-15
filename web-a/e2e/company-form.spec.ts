import { test, expect } from './fixtures.ts'

test('invalid company submit explains errors and focuses the first invalid field', async ({ page, api }) => {
  await page.goto('/companies')
  const submit = page.getByRole('button', { name: 'Create company', exact: true })
  await expect(submit).toBeEnabled()
  await submit.focus()
  await submit.press('Enter')
  const name = page.getByLabel('Company name', { exact: true })
  const email = page.getByLabel('Org admin email', { exact: true })
  await expect(name).toBeFocused()
  await expect(name).toHaveAttribute('aria-invalid', 'true')
  await expect(name).toHaveAccessibleDescription('Enter a company name')
  await expect(email).toHaveAccessibleDescription('Enter the org admin email')
  await expect(page.getByRole('alert')).toHaveCount(5)
  expect(api.requests.filter((request) => request.method === 'POST')).toEqual([])
  await name.fill('Synthetic Company')
  await page.getByLabel('Phone number', { exact: true }).fill('091 234 5678')
  await page.getByRole('radio', { name: 'Pro', exact: true }).check()
  await email.fill('invalid-email')
  await page.getByLabel('Initial admin password', { exact: true }).fill('secret-1')
  await submit.click()
  await expect(email).toBeFocused()
  await expect(email).toHaveAccessibleDescription('Enter a valid email address')
  expect(api.requests.filter((request) => request.method === 'POST')).toEqual([])
})

test('valid company submit normalizes once, locks pending submission, and resets on success', async ({ page, api }) => {
  api.holdCreate = true
  await page.goto('/companies')
  await page.getByLabel('Company name', { exact: true }).fill(' Synthetic Company ')
  await page.getByLabel('Phone number', { exact: true }).fill('091 234 5678')
  await page.getByRole('radio', { name: 'Pro', exact: true }).check()
  await page.getByLabel('Org admin email', { exact: true }).fill(' Admin@Example.test ')
  await page.getByLabel('Admin display name (optional)', { exact: true }).fill(' Operator ')
  await page.getByLabel('Initial admin password', { exact: true }).fill('secret-1')
  const submit = page.getByRole('button', { name: 'Create company', exact: true })
  await submit.click()
  await expect(submit).toBeDisabled()
  await page.getByLabel('Initial admin password', { exact: true }).press('Enter')
  await expect.poll(() => api.requests.filter((request) => request.method === 'POST')).toEqual([{
    method: 'POST', path: '/api/admin/companies', body: {
      name: 'Synthetic Company', phone: '+84912345678', tier: 'pro',
      admin_email: 'admin@example.test', admin_password: 'secret-1', admin_display_name: 'Operator',
    },
  }])
  api.createGate.resolve()
  await expect(page.getByText('Created Synthetic Company. admin@example.test must change password on first login.', { exact: true })).toBeVisible()
  await expect(page.getByLabel('Company name', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('Org admin email', { exact: true })).toHaveValue('')
  await expect(page.getByRole('radio', { name: 'Pro', exact: true })).not.toBeChecked()
  await expect(submit).toBeEnabled()
})
