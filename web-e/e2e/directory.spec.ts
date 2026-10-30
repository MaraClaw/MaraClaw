import { expect, test } from '@playwright/test'

import { dana, peoplePageRoute, prepareDirectory } from './directory-fixture'
import { registerPeoplePaginationTests } from './directory-pagination-cases'
import { directoryPath, orgDepartment } from './org-departments-fixture'

registerPeoplePaginationTests()

for (const entry of [
  { name: 'People', path: '/app/directory', tab: 'People', query: '' },
  { name: 'Departments', path: directoryPath, tab: 'Departments', query: '' },
  { name: 'a stale synced link', path: '/app/directory?tab=synced&q=engineer', tab: 'People', query: 'engineer' },
]) {
  test(`opens ${entry.name} without retired provider requests`, async ({ page }) => {
    const { retiredRequests, requests } = await prepareDirectory(page)

    await page.goto(entry.path)

    if (entry.tab === 'Departments') {
      await expect(page.getByRole('heading', { name: orgDepartment.name, exact: true })).toBeVisible()
    } else {
      await expect(page.getByText('Dana Woods', { exact: true })).toBeVisible()
      await expect(page.getByRole('searchbox', { name: 'Search directory' })).toHaveValue(entry.query)
      if (entry.query) await expect(page.getByText('Rowan Chen', { exact: true })).toHaveCount(0)
    }
    expect(retiredRequests).toEqual([])
    expect(requests.length).toBe(entry.tab === 'Departments' ? 0 : 1)
    const sections = page.getByRole('navigation', { name: 'Directory sections' })
    await expect(sections.getByRole('button')).toHaveCount(2)
    await expect(sections.getByRole('button', { name: entry.tab, exact: true })).toHaveAttribute('aria-current', 'true')
    await expect(page.getByRole('region', { name: 'Provider-synced departments' })).toHaveCount(0)
    expect(new URL(page.url()).searchParams.get('q') ?? '').toBe(entry.query)
  })
}

for (const query of ['  DANA  ', 'dana@example', 'Dana Woods', 'engineer', 'org_admin']) {
  test(`People search matches ${JSON.stringify(query)} without provider requests`, async ({ page }) => {
    const { retiredRequests, requests } = await prepareDirectory(page)
    await page.goto('/app/directory')
    await expect(page.getByText('Rowan Chen', { exact: true })).toBeVisible()

    await page.getByRole('searchbox', { name: 'Search directory' }).fill(query)

    await expect(page.getByText('Dana Woods', { exact: true })).toBeVisible()
    await expect(page.getByText('Rowan Chen', { exact: true })).toHaveCount(0)
    expect(new URL(page.url()).searchParams.get('q')).toBe(query)
    expect(retiredRequests).toEqual([])
    expect(requests.at(-1)?.searchParams.get('search')).toBe(query.trim().toLowerCase())
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

test('People uses email when a display name is absent', async ({ page }) => {
  // Given
  await prepareDirectory(page)
  await page.route(peoplePageRoute, (route) => route.fulfill({ json: {
    items: [{ ...dana, display_name: '', email: 'email-only@example.test' }], next_cursor: null,
  } }))
  // When
  await page.goto('/app/directory')
  // Then
  await expect(page.locator('[data-slot="card-title"]', { hasText: 'email-only@example.test' })).toBeVisible()
})

test('People does not render or search retired account usernames', async ({ page }) => {
  // Given
  await prepareDirectory(page)
  await page.route(peoplePageRoute, (route) => route.fulfill({ json: {
    items: new URL(route.request().url()).searchParams.get('search') ? [] : [{ ...dana, username: 'retired-account-handle' }],
    next_cursor: null,
  } }))
  await page.goto('/app/directory')
  await expect(page.getByText('Dana Woods', { exact: true })).toBeVisible()
  await expect(page.getByText('retired-account-handle', { exact: false })).toHaveCount(0)
  // When
  await page.getByRole('searchbox', { name: 'Search directory' }).fill('retired-account-handle')
  // Then
  await expect(page.getByText('Dana Woods', { exact: true })).toHaveCount(0)
  await expect(page.getByText('No people match', { exact: false })).toBeVisible()
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
        expect(state.rosterRequests).toEqual([`/api/departments/${orgDepartment.id}/members`])
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
