import { readFileSync } from 'node:fs'
import { test, expect } from './fixtures.ts'

const COMPANIES_PAGE = /\/src\/pages\/companies\.tsx/
const DOWNLOAD_FAILED = 'This page couldn’t be downloaded'
const paymentsQueue = (page: import('@playwright/test').Page) => page.getByRole('heading', { name: 'No pending payments', exact: true })

test('a Companies page that fails to download keeps the admin navigation, and Try again loads it without a reload', async ({ page }, testInfo) => {
  let downloads = 0
  await page.route(COMPANIES_PAGE, (route) => {
    downloads++
    return route.abort()
  })
  const documents: string[] = []
  page.on('request', (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documents.push(new URL(request.url()).pathname)
  })
  await page.goto('/payments')
  await expect(paymentsQueue(page)).toBeVisible()

  await page.getByRole('link', { name: 'Companies', exact: true }).click()
  await expect(page).toHaveURL('/companies')
  const alert = page.getByRole('alert')
  await expect(alert).toContainText(DOWNLOAD_FAILED)
  await expect(alert).toContainText('Check your connection and try again.')
  await expect(page.getByRole('link', { name: 'Companies', exact: true })).toHaveAttribute('aria-current', 'page')
  await page.screenshot({ path: testInfo.outputPath('companies-download-failed.png') })

  await page.getByRole('button', { name: 'Try again', exact: true }).click()
  await expect.poll(() => downloads).toBe(2)
  await expect(page.getByRole('button', { name: 'Try again', exact: true })).toBeVisible()
  await expect(alert).toContainText(DOWNLOAD_FAILED)

  await page.getByRole('link', { name: 'Payments', exact: true }).click()
  await expect(page).toHaveURL('/payments')
  await expect(paymentsQueue(page)).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)

  await page.goBack()
  await expect(page).toHaveURL('/companies')
  await expect.poll(() => downloads).toBe(3)
  await expect(alert).toContainText(DOWNLOAD_FAILED)

  await page.unroute(COMPANIES_PAGE)
  await page.getByRole('button', { name: 'Try again', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Companies', exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(documents).toEqual(['/payments'])
})

test('a page that throws while rendering shows the page error without its details, and navigation recovers', async ({ page }, testInfo) => {
  await page.route(COMPANIES_PAGE, (route) => route.fulfill({
    contentType: 'text/javascript',
    body: "export function CompaniesPage() { throw new Error('Synthetic render failure') }",
  }))
  await page.goto('/companies')
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('This page stopped working')
  await expect(alert).toContainText('Try again, or open another page.')
  await expect(alert).not.toContainText(DOWNLOAD_FAILED)
  await expect(page.getByText(/Synthetic render failure|CompaniesPage|companies\.tsx/)).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('companies-page-error.png') })

  await page.getByRole('button', { name: 'Try again', exact: true }).click()
  await expect(alert).toContainText('This page stopped working')
  await page.getByRole('link', { name: 'Payments', exact: true }).click()
  await expect(paymentsQueue(page)).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
})

// Every lazily routed page module except sign-in and Settings, the forced password change destination.
const routedPages = Array.from(
  readFileSync(new URL('../src/routes/index.tsx', import.meta.url), 'utf8').matchAll(/import\('@\/pages\/([\w-]+)'\)/g),
  (match) => match[1],
).filter((name) => name !== 'login' && name !== 'settings')
const OTHER_PAGES = new RegExp(`/src/pages/(${routedPages.join('|')})\\.tsx(\\?|$)`)

test.describe('anonymous', () => {
  test.use({ actor: 'anonymous' })
  test('sign-in works while every other page module is missing', async ({ page }) => {
    expect(routedPages).toContain('companies')
    await page.route(OTHER_PAGES, (route) => route.abort())
    await page.goto('/companies')
    await expect(page).toHaveURL('/login')
    await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeEnabled()
    await expect(page.getByRole('alert')).toHaveCount(0)
  })
})

test.describe('temporary password', () => {
  test.use({ actor: 'forced' })
  test('the forced password change works while every other page module is missing', async ({ page }) => {
    expect(routedPages).toContain('companies')
    await page.route(OTHER_PAGES, (route) => route.abort())
    await page.goto('/companies')
    await expect(page).toHaveURL('/settings')
    await expect(page.getByLabel('Current password', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Update password', exact: true })).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
  })
})
