import { expect, test } from '@playwright/test'
import { objective, prepareDashboardPages, relationship } from './dashboard-pages-fixture'
import { originalAgent, originalToken, replacementToken, writeFromOtherTab } from './session-fencing-fixture'

for (const domain of ['objectives', 'relationships'] as const) {
  const path = domain === 'objectives' ? '/app/okr' : `/app/agents/${originalAgent.id}/relationships`
  const endpoint = domain === 'objectives' ? '**/api/okr/objectives/page?**' : `**/api/agents/${originalAgent.id}/relationships/page?**`
  const navigation = domain === 'objectives' ? 'Objective pages' : 'Relationship pages'
  const first = domain === 'objectives' ? 'First objective' : 'First'
  const replacement = domain === 'objectives' ? 'Replacement objective' : 'Replacement person'
  test(`${domain} session replacement retires a held continuation and starts page one`, async ({ page }) => {
    await prepareDashboardPages(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const completed = Promise.withResolvers<void>()
    await page.route(endpoint, async (route) => {
      if (!new URL(route.request().url()).searchParams.has('cursor')) return route.fallback()
      expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
      requested.resolve()
      await release.promise
      try {
        await route.fulfill({ json: domain === 'objectives'
          ? { items: [objective('Retired result')], next_cursor: null }
          : { items: [relationship('Retired result')], next_cursor: null, has_more: false } })
      } finally { completed.resolve() }
    })
    try {
      await page.goto(path)
      await expect(page.getByText(first, { exact: true })).toBeVisible()
      await page.getByRole('navigation', { name: navigation }).getByRole('button', { name: 'Next' }).click()
      await requested.promise
      await writeFromOtherTab(page, replacementToken)
      await expect(page.getByText(replacement, { exact: true })).toBeVisible()
      release.resolve()
      await completed.promise
      await expect(page.getByText('Retired result', { exact: true })).toHaveCount(0)
      await expect(page.getByRole('navigation', { name: navigation }).getByRole('status')).toHaveText('Page 1')
    } finally { release.resolve() }
  })

  test(`${domain} mutation cancels an obsolete continuation before resetting to page one`, async ({ page }) => {
    await prepareDashboardPages(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const completed = Promise.withResolvers<void>()
    await page.route(endpoint, async (route) => {
      if (!new URL(route.request().url()).searchParams.has('cursor')) return route.fallback()
      requested.resolve()
      await release.promise
      try {
        await route.fulfill({ json: domain === 'objectives'
          ? { items: [objective('Retired result')], next_cursor: null }
          : { items: [relationship('Retired result')], next_cursor: null, has_more: false } })
      } finally { completed.resolve() }
    })
    try {
      await page.goto(path)
      await expect(page.getByText(first, { exact: true })).toBeVisible()
      await page.getByRole('navigation', { name: navigation }).getByRole('button', { name: 'Next' }).click()
      await requested.promise
      if (domain === 'objectives') await page.getByLabel('New company objective').fill('Created objective')
      else await page.getByLabel('Choose a person').selectOption('Added')
      await page.getByRole('button', { name: 'Add', exact: true }).click()
      await expect(page.getByText(domain === 'objectives' ? 'Created objective' : 'First', { exact: true })).toBeVisible()
      release.resolve()
      await completed.promise
      await expect(page.getByText('Retired result', { exact: true })).toHaveCount(0)
      await expect(page.getByRole('navigation', { name: navigation }).getByRole('status')).toHaveText('Page 1')
    } finally { release.resolve() }
  })

  test(`${domain} rejects an invalid continuation with an explicit restart`, async ({ page }) => {
    await prepareDashboardPages(page)
    await page.route(endpoint, async (route) => {
      if (!new URL(route.request().url()).searchParams.has('cursor')) return route.fallback()
      return route.fulfill({ status: 400, json: { detail: 'Invalid cursor' } })
    })
    await page.goto(path)
    await expect(page.getByText(first, { exact: true })).toBeVisible()
    await page.getByRole('navigation', { name: navigation }).getByRole('button', { name: 'Next' }).click()
    await expect(page.getByRole('alert')).toHaveText('Invalid cursor')
    await page.getByRole('button', { name: `Restart ${domain}`, exact: true }).click()
    await expect(page.getByText(first, { exact: true })).toBeVisible()
    await expect(page.getByRole('navigation', { name: navigation }).getByRole('status')).toHaveText('Page 1')
  })
}
