import { expect, test } from '@playwright/test'

test('landing shows the product and auth entry points', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('link', { name: /sign in/i }).first()).toBeVisible()
  await expect(page.locator('body')).toContainText(/MaraClaw|OpenClaw|digital/i)
})

test('login page is reachable from marketing', async ({ page }) => {
  await page.goto('/login')
  await expect(page.locator('input').first()).toBeVisible()
})
