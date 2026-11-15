import { test, expect } from './fixtures.ts'
import { paymentAliases } from './synthetic-api.ts'

test('platform admins can enter Companies with its current navigation link', async ({ page }) => {
  await page.goto('/companies')
  await expect(page.getByRole('heading', { name: 'Companies', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Companies', exact: true })).toHaveAttribute('aria-current', 'page')
})

test.describe('organization admin', () => {
  test.use({ actor: 'org_admin' })
  test('shared activity works while company and payment access stays blocked', async ({ page, api }) => {
    await page.goto('/activity')
    await expect(page.getByRole('heading', { name: 'Recent activity', exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Companies', exact: true })).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'Payments', exact: true })).toHaveCount(0)
    for (const path of ['/companies', ...paymentAliases.map((alias) => alias.path)]) {
      await page.goto(path)
      await expect(page).toHaveURL('/')
    }
    expect(api.requests.filter((request) => request.path.startsWith('/api/admin/') || request.path.includes('/transfers/'))).toEqual([])
  })
})

for (const actor of ['member', 'anonymous'] as const) test.describe(actor, () => {
  test.use({ actor })
  test('cannot enter the admin console', async ({ page, api }) => {
    await page.goto('/companies')
    await expect(page).toHaveURL('/login')
    await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible()
    expect(new Set(api.requests.map((request) => request.path))).toEqual(new Set(actor === 'member' ? ['/api/auth/me'] : []))
  })
})

test.describe('temporary password', () => {
  test.use({ actor: 'forced' })
  test('redirects to Settings and locks operational navigation without protected reads', async ({ page, api }) => {
    await page.goto('/companies')
    await expect(page).toHaveURL('/settings')
    await expect(page.getByLabel('Current password', { exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Companies', exact: true })).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'Payments', exact: true })).toHaveCount(0)
    await expect(page.getByRole('link', { name: 'Settings', exact: true })).toHaveAttribute('aria-current', 'page')
    expect(new Set(api.requests.map((request) => request.path))).toEqual(new Set(['/api/auth/me']))
  })
})
