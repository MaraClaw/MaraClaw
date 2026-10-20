import { expect, test, type Page } from '@playwright/test'

import type { DirectoryUser } from '../src/lib/directory-api'
import { directoryPath, localDepartment, prepareLocalDepartments } from './local-departments-fixture'

const people = [
  { id: 'dana', display_name: 'Dana Woods', username: 'dana.woods', email: 'dana@example.test', title: 'Product engineer', role: 'org_admin' },
  { id: 'rowan', display_name: 'Rowan Chen', username: 'rowan.chen', email: 'rowan@example.test', title: 'Marketing designer', role: 'member' },
] satisfies readonly DirectoryUser[]

async function prepareDirectory(page: Page) {
  const state = await prepareLocalDepartments(page, 'org_admin')
  const retiredRequests: string[] = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (path.startsWith('/api/enterprise/org/')) retiredRequests.push(path)
  })
  await page.route('**/api/org/users', (route) => route.fulfill({ json: people }))
  return { state, retiredRequests }
}

for (const entry of [
  { name: 'People', path: '/app/directory', tab: 'People', query: '' },
  { name: 'Departments', path: directoryPath, tab: 'Departments', query: '' },
  { name: 'a stale synced link', path: '/app/directory?tab=synced&q=engineer', tab: 'People', query: 'engineer' },
]) {
  test(`opens ${entry.name} without retired provider requests`, async ({ page }) => {
    const { retiredRequests } = await prepareDirectory(page)

    await page.goto(entry.path)

    if (entry.tab === 'Departments') {
      await expect(page.getByRole('heading', { name: localDepartment.name, exact: true })).toBeVisible()
    } else {
      await expect(page.getByText('Dana Woods', { exact: true })).toBeVisible()
      await expect(page.getByRole('searchbox', { name: 'Search directory' })).toHaveValue(entry.query)
      if (entry.query) await expect(page.getByText('Rowan Chen', { exact: true })).toHaveCount(0)
    }
    expect(retiredRequests).toEqual([])
    const sections = page.getByRole('navigation', { name: 'Directory sections' })
    await expect(sections.getByRole('button')).toHaveCount(2)
    await expect(sections.getByRole('button', { name: entry.tab, exact: true })).toHaveAttribute('aria-current', 'true')
    await expect(page.getByRole('region', { name: 'Provider-synced departments' })).toHaveCount(0)
    expect(new URL(page.url()).searchParams.get('q') ?? '').toBe(entry.query)
  })
}

for (const query of ['  DANA  ', 'dana@example', 'dana.woods', 'engineer', 'org_admin']) {
  test(`People search matches ${JSON.stringify(query)} without provider requests`, async ({ page }) => {
    const { retiredRequests } = await prepareDirectory(page)
    await page.goto('/app/directory')
    await expect(page.getByText('Rowan Chen', { exact: true })).toBeVisible()

    await page.getByRole('searchbox', { name: 'Search directory' }).fill(query)

    await expect(page.getByText('Dana Woods', { exact: true })).toBeVisible()
    await expect(page.getByText('Rowan Chen', { exact: true })).toHaveCount(0)
    expect(new URL(page.url()).searchParams.get('q')).toBe(query)
    expect(retiredRequests).toEqual([])
  })
}

test('People search can recover from no matches', async ({ page }) => {
  const { retiredRequests } = await prepareDirectory(page)
  await page.goto('/app/directory?q=unmatched-person')
  await expect(page.getByText('No people match', { exact: false })).toBeVisible()

  await page.getByRole('searchbox', { name: 'Search directory' }).clear()

  await expect(page.getByText('Dana Woods', { exact: true })).toBeVisible()
  await expect(page.getByText('Rowan Chen', { exact: true })).toBeVisible()
  expect(new URL(page.url()).searchParams.has('q')).toBe(false)
  expect(retiredRequests).toEqual([])
})

for (const width of [390, 1440]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width}px ${colorScheme} directory`, () => {
      test.use({ viewport: { width, height: 900 }, colorScheme })

      test('local roster and rename work between retained tabs while preserving People search', async ({ page }, testInfo) => {
        const { state, retiredRequests } = await prepareDirectory(page)
        const errors: string[] = []
        page.on('pageerror', (error) => errors.push(error.message))
        await page.goto('/app/directory?q=engineer')
        await expect(page.getByText('Dana Woods', { exact: true })).toBeVisible()

        await page.getByRole('button', { name: 'Departments', exact: true }).click()
        await page.getByRole('button', { name: 'Show members for Engineering', exact: true }).click()
        await expect(page.getByRole('region', { name: 'Engineering members', exact: true }).getByRole('listitem')).toHaveCount(4)
        await page.getByRole('button', { name: 'Edit name for Engineering', exact: true }).click()
        await page.getByLabel('Department name', { exact: true }).fill('  Product engineering  ')
        await page.getByLabel('Department name', { exact: true }).press('Enter')

        await expect(page.getByRole('button', { name: 'Edit name for Product engineering', exact: true })).toBeFocused()
        await expect(page.getByRole('region', { name: 'Product engineering members', exact: true }).getByRole('listitem')).toHaveCount(4)
        expect(state.patches).toEqual([{ name: 'Product engineering' }])
        expect(state.rosterRequests).toEqual([`/api/departments/${localDepartment.id}/members`])
        await page.screenshot({ path: testInfo.outputPath('local-renamed.png'), fullPage: true })
        await page.getByRole('button', { name: 'People', exact: true }).click()
        await expect(page.getByRole('searchbox', { name: 'Search directory' })).toHaveValue('engineer')
        await expect(page.getByText('Dana Woods', { exact: true })).toBeVisible()
        await expect(page.getByText('Rowan Chen', { exact: true })).toHaveCount(0)
        expect(new URL(page.url()).searchParams.get('tab')).toBeNull()
        expect(retiredRequests).toEqual([])
        expect(state.privilegedRequests).toBe(0)
        expect(errors).toEqual([])
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
        await page.screenshot({ path: testInfo.outputPath('people-filtered.png'), fullPage: true })
      })
    })
  }
}
