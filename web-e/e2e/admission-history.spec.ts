import { expect, test } from '@playwright/test'

import { historyCursor, historyPath, historyRows, prepareHistory } from './admission-history-fixture'
import { originalToken, tokenKey } from './session-fencing-fixture'

for (const width of [390, 1440]) {
  for (const theme of ['light', 'dark'] as const) {
    test(`history filter and keyset controls remain accessible at ${width}px in ${theme}`, async ({ page }, testInfo) => {
      // Given
      const fixture = await prepareHistory(page)
      await page.setViewportSize({ width, height: 900 })
      await page.addInitScript((value) => localStorage.setItem('maraclaw-theme', value), theme)
      await page.goto('/app/admissions')
      const list = page.getByRole('list', { name: 'Admission requests' })
      await expect(list.getByRole('listitem')).toHaveCount(2)
      const filter = page.getByRole('combobox', { name: 'Request status' })
      await filter.focus()
      await expect(filter).toBeFocused()
      await expect(filter).not.toHaveCSS('box-shadow', 'none')
      expect((await filter.boundingBox())?.height).toBeGreaterThanOrEqual(44)
      // When
      await page.getByRole('button', { name: 'Older requests' }).focus()
      await page.getByRole('button', { name: 'Older requests' }).press('Enter')
      // Then
      await expect(page.getByText('Page 2', { exact: true })).toBeVisible()
      await expect(list.getByRole('heading', { name: 'History rejected company' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Older requests' })).toBeDisabled()
      expect(fixture.calls.at(-1)?.cursor).toBe(historyCursor)
      await page.getByRole('button', { name: 'Back', exact: true }).click()
      await expect(list.getByRole('heading', { name: 'History pending company' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      await page.screenshot({ path: testInfo.outputPath(`history-${width}-${theme}.png`), fullPage: true })
    })
  }
}

for (const status of ['pending', 'approved', 'rejected', 'withdrawn'] as const) {
  test(`history filters ${status} and returns to all without installing membership`, async ({ page }) => {
    // Given
    const fixture = await prepareHistory(page)
    await page.goto('/app/admissions')
    await expect(page.getByRole('listitem')).toHaveCount(2)
    // When
    await page.getByLabel('Request status').selectOption(status)
    // Then
    await expect(page.getByRole('listitem')).toHaveCount(1)
    await expect(page.getByRole('heading', { name: `History ${status} company` })).toBeVisible()
    expect(fixture.calls.at(-1)?.status).toBe(status)
    await page.getByLabel('Request status').selectOption('all')
    await expect(page.getByRole('listitem')).toHaveCount(2)
    expect(fixture.calls[0]?.status).toBeNull()
    expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
  })
}

test('refresh from an older page fetches a fresh cached root and drops the cursor stack', async ({ page }) => {
  // Given
  const fixture = await prepareHistory(page)
  await page.goto('/app/admissions')
  await page.getByRole('button', { name: 'Older requests' }).click()
  await expect(page.getByText('Page 2', { exact: true })).toBeVisible()
  fixture.pages.set('all:root', { items: [], next_cursor: null })
  // When
  await page.getByRole('button', { name: 'Refresh status' }).click()
  // Then
  await expect(page.getByText('No admission requests yet.')).toBeVisible()
  await expect(page.getByText('Page 1', { exact: true })).toBeVisible()
  await expect(page.getByRole('listitem')).toHaveCount(0)
  expect(fixture.calls.filter((call) => call.cursor === null)).toHaveLength(2)
  await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled()
})

test('filter change from an older page resets to its first page and distinguishes filtered empty', async ({ page }) => {
  // Given
  const fixture = await prepareHistory(page)
  fixture.pages.set('withdrawn:root', { items: [], next_cursor: null })
  await page.goto('/app/admissions')
  await page.getByRole('button', { name: 'Older requests' }).click()
  await expect(page.getByText('Page 2', { exact: true })).toBeVisible()
  // When
  await page.getByLabel('Request status').selectOption('withdrawn')
  // Then
  await expect(page.getByText('No withdrawn requests match this filter.')).toBeVisible()
  await expect(page.getByText('No admission requests yet.')).toHaveCount(0)
  await expect(page.getByText('Page 1', { exact: true })).toBeVisible()
  expect(fixture.calls.at(-1)).toMatchObject({ status: 'withdrawn', cursor: null })
  await page.getByRole('button', { name: 'Show all statuses' }).click()
  await expect(page.getByRole('listitem')).toHaveCount(2)
})

test('an empty older page does not claim that the applicant has never requested admission', async ({ page }) => {
  // Given
  const fixture = await prepareHistory(page)
  fixture.pages.set(`all:${historyCursor}`, { items: [], next_cursor: null })
  await page.goto('/app/admissions')
  // When
  await page.getByRole('button', { name: 'Older requests' }).click()
  // Then
  await expect(page.getByText('No more requests on this page. Go back or refresh to see the latest requests.')).toBeVisible()
  await expect(page.getByText('No admission requests yet.')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeEnabled()
})

test('invalid later cursor returns to a freshly requested root without stale rows', async ({ page }) => {
  // Given
  const fixture = await prepareHistory(page)
  await page.goto('/app/admissions')
  await expect(page.getByRole('listitem')).toHaveCount(2)
  fixture.pages.set('all:root', { items: historyRows.slice(3), next_cursor: null })
  await page.route(`**${historyPath}*`, async (route) => {
    if (!new URL(route.request().url()).searchParams.has('cursor')) return route.fallback()
    await route.fulfill({ status: 400, json: { detail: 'Invalid cursor' } })
  })
  // When
  await page.getByRole('button', { name: 'Older requests' }).click()
  // Then
  await expect(page.getByText('History changed. Showing the latest requests.')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'History withdrawn company' })).toBeVisible()
  await expect(page.getByRole('listitem')).toHaveCount(1)
  await expect(page.getByText('Page 1', { exact: true })).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(fixture.calls.filter((call) => call.cursor === null)).toHaveLength(2)
})

for (const status of [403, 503]) {
  test(`history ${status} error hides cached rows and refresh recovers`, async ({ page }) => {
    // Given
    await prepareHistory(page)
    await page.goto('/app/admissions')
    await expect(page.getByRole('listitem')).toHaveCount(2)
    await page.route(`**${historyPath}*`, (route) => route.fulfill({ status, json: { detail: 'History access refused' } }))
    // When
    await page.getByRole('button', { name: 'Refresh status' }).click()
    // Then
    await expect(page.getByRole('alert')).toContainText(status === 403 ? 'History access refused' : 'Unable to load requests')
    await expect(page.getByRole('listitem')).toHaveCount(0)
    await page.unroute(`**${historyPath}*`)
    await page.route(`**${historyPath}*`, (route) => route.fulfill({ json: { items: historyRows.slice(0, 1), next_cursor: null } }))
    await page.getByRole('button', { name: 'Refresh status' }).click()
    await expect(page.getByRole('listitem')).toHaveCount(1)
    await expect(page.getByRole('alert')).toHaveCount(0)
  })
}
