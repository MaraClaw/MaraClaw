import { expect, test } from '@playwright/test'

import { dana, peoplePageRoute, prepareDirectory, rowan } from './directory-fixture'
import { replacementToken, replacementUser, writeFromOtherTab } from './session-fencing-fixture'

const cursor = 'opaque+/=&? %_ cursor'

export function registerPeoplePaginationTests() {
  for (const width of [390, 1440]) {
    for (const colorScheme of ['light', 'dark'] as const) {
      test.describe(`${width}px ${colorScheme} paged People`, () => {
        test.use({ viewport: { width, height: 900 }, colorScheme })
        test('traverses tied names and stops at the terminal page', async ({ page }, testInfo) => {
          // Given
          const { requests, retiredRequests } = await prepareDirectory(page)
          const release = Promise.withResolvers<void>()
          const requested = Promise.withResolvers<void>()
          await page.route(peoplePageRoute, async (route) => {
            const next = new URL(route.request().url()).searchParams.get('cursor')
            if (next) {
              expect(next).toBe(cursor)
              requested.resolve()
              await release.promise
            }
            await route.fulfill({ json: next
              ? { items: [{ ...rowan, display_name: dana.display_name }], next_cursor: null }
              : { items: [dana], next_cursor: cursor } })
          })
          await page.goto('/app/directory')
          await expect(page.getByText(dana.email, { exact: true })).toBeVisible()
          const more = page.getByRole('button', { name: 'Load more', exact: true })
          await more.focus()
          await expect(more).toBeFocused()
          await page.screenshot({ path: testInfo.outputPath('load-more-focused.png') })
          // When
          try {
            await more.press('Enter')
            await requested.promise
            await expect(page.getByRole('button', { name: 'Loading more…' })).toBeDisabled()
            await expect(page.getByText(dana.email, { exact: true })).toBeVisible()
            await page.screenshot({ path: testInfo.outputPath('loading-next.png') })
          } finally { release.resolve() }
          // Then
          await expect(page.getByText(rowan.email, { exact: true })).toBeVisible()
          await expect(page.getByText(dana.display_name, { exact: true })).toHaveCount(2)
          await expect(page.getByRole('button', { name: 'Load more' })).toHaveCount(0)
          expect(requests.map((url) => url.searchParams.get('cursor'))).toEqual([null, cursor])
          expect(retiredRequests).toEqual([])
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
          await page.screenshot({ path: testInfo.outputPath('terminal-page.png') })
        })
      })
    }
  }

  test('shows initial loading before a People page arrives', async ({ page }, testInfo) => {
    // Given
    await prepareDirectory(page)
    const release = Promise.withResolvers<void>()
    const requested = Promise.withResolvers<void>()
    await page.route(peoplePageRoute, async (route) => {
      requested.resolve()
      await release.promise
      await route.fulfill({ json: { items: [dana], next_cursor: null } })
    })
    // When
    try {
      await page.goto('/app/directory')
      await requested.promise
      // Then
      await expect(page.getByText('No people in this company yet.')).toHaveCount(0)
      await expect(page.getByText(dana.email, { exact: true })).toHaveCount(0)
      await page.screenshot({ path: testInfo.outputPath('initial-loading.png') })
    } finally { release.resolve() }
    await expect(page.getByText(dana.email, { exact: true })).toBeVisible()
  })

  for (const failure of ['http', 'malformed'] as const) {
    test(`retries an initial ${failure} failure instead of treating it as an empty directory`, async ({ page }, testInfo) => {
      // Given
      await prepareDirectory(page)
      let attempts = 0
      await page.route(peoplePageRoute, (route) => {
        attempts++
        if (attempts > 1) return route.fulfill({ json: { items: [dana], next_cursor: null } })
        return route.fulfill(failure === 'http'
          ? { status: 503, json: { detail: 'People unavailable' } }
          : { json: { items: [{ id: 'invalid' }], next_cursor: null } })
      })
      await page.goto('/app/directory')
      await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible()
      await expect(page.getByText('No people in this company yet.')).toHaveCount(0)
      await page.screenshot({ path: testInfo.outputPath('initial-error.png') })
      // When
      await page.getByRole('button', { name: 'Retry', exact: true }).click()
      // Then
      await expect(page.getByText(dana.email, { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Retry' })).toHaveCount(0)
      expect(attempts).toBe(2)
    })
  }

  test('keeps loaded People when a next page fails and retries the same cursor', async ({ page }, testInfo) => {
    // Given
    const { requests } = await prepareDirectory(page)
    let nextAttempts = 0
    await page.route(peoplePageRoute, (route) => {
      if (!new URL(route.request().url()).searchParams.has('cursor')) {
        return route.fulfill({ json: { items: [dana], next_cursor: cursor } })
      }
      nextAttempts++
      return route.fulfill(nextAttempts === 1
        ? { status: 503, json: { detail: 'temporary backend detail' } }
        : { json: { items: [rowan], next_cursor: null } })
    })
    await page.goto('/app/directory')
    await page.getByRole('button', { name: 'Load more' }).click()
    await expect(page.getByRole('alert')).toHaveText('Unable to load more people.')
    await expect(page.getByText(dana.email, { exact: true })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('next-page-error.png') })
    // When
    await page.getByRole('button', { name: 'Retry', exact: true }).click()
    // Then
    await expect(page.getByText(rowan.email, { exact: true })).toBeVisible()
    await expect(page.getByText(dana.email, { exact: true })).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
    expect(requests.map((url) => url.searchParams.get('cursor'))).toEqual([null, cursor, cursor])
  })

  for (const phase of ['initial', 'next'] as const) {
    test(`discards a late ${phase} page after search changes`, async ({ page }) => {
      // Given
      const { requests } = await prepareDirectory(page)
      const requested = Promise.withResolvers<void>()
      const release = Promise.withResolvers<void>()
      const finished = Promise.withResolvers<void>()
      await page.route(peoplePageRoute, async (route) => {
        const url = new URL(route.request().url())
        if (url.searchParams.get('search')) return route.fallback()
        if (phase === 'next' && !url.searchParams.has('cursor')) {
          return route.fulfill({ json: { items: [dana], next_cursor: cursor } })
        }
        requested.resolve()
        await release.promise
        try { await route.fulfill({ json: { items: [{ ...dana, display_name: 'Late old person' }], next_cursor: cursor } }) }
        finally { finished.resolve() }
      })
      try {
        await page.goto('/app/directory')
        if (phase === 'next') await page.getByRole('button', { name: 'Load more' }).click()
        await requested.promise
        // When
        await page.getByRole('searchbox', { name: 'Search directory' }).fill('  ROWAN  ')
        await expect(page.getByText(rowan.email, { exact: true })).toBeVisible()
      } finally { release.resolve() }
      await finished.promise
      // Then
      await expect(page.getByText('Late old person', { exact: true })).toHaveCount(0)
      await expect(page.getByText(dana.email, { exact: true })).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Load more' })).toHaveCount(0)
      expect(requests.at(-1)?.searchParams.get('search')).toBe('rowan')
      expect(requests.at(-1)?.searchParams.has('cursor')).toBe(false)
    })
  }

  test('discards a late next page after cross-tab session replacement', async ({ page }) => {
    // Given
    await prepareDirectory(page, 'member')
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const finished = Promise.withResolvers<void>()
    await page.route(peoplePageRoute, async (route) => {
      if (route.request().headers().authorization === `Bearer ${replacementToken}`) {
        expect(new URL(route.request().url()).searchParams.has('cursor')).toBe(false)
        return route.fulfill({ json: { items: [replacementUser], next_cursor: null } })
      }
      if (!new URL(route.request().url()).searchParams.has('cursor')) {
        return route.fulfill({ json: { items: [dana], next_cursor: cursor } })
      }
      requested.resolve()
      await release.promise
      try { await route.fulfill({ json: { items: [rowan], next_cursor: null } }) }
      finally { finished.resolve() }
    })
    try {
      await page.goto('/app/directory')
      await page.getByRole('button', { name: 'Load more' }).click()
      await requested.promise
      // When
      await writeFromOtherTab(page, replacementToken)
      await expect(page.getByText(replacementUser.email, { exact: true })).toBeVisible()
    } finally { release.resolve() }
    await finished.promise
    // Then
    await expect(page.getByText(dana.email, { exact: true })).toHaveCount(0)
    await expect(page.getByText(rowan.email, { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Load more' })).toHaveCount(0)
  })
}
