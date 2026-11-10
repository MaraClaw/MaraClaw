import { expect, test } from '@playwright/test'
import { currentPeriod, objective, objectiveCursor, prepareDashboardPages, previousPeriod, relationship, relationshipCursor } from './dashboard-pages-fixture'
import { originalAgent, replacementAgent } from './session-fencing-fixture'

test('OKR shows one complete page, uses explicit traversal and restarts after creating an objective', async ({ page }) => {
  const { objectiveRequests } = await prepareDashboardPages(page)
  await page.goto('/app/okr')
  await expect(page.getByText('First objective', { exact: true })).toBeVisible()
  await expect(page.getByText('First objective result', { exact: true })).toBeVisible()
  expect(objectiveRequests).toEqual([null])
  const nav = page.getByRole('navigation', { name: 'Objective pages' })
  await expect(nav.getByRole('button', { name: 'Previous' })).toBeDisabled()
  await nav.getByRole('button', { name: 'Next' }).click()
  await expect(page.getByText('Second objective', { exact: true })).toBeVisible()
  await expect(page.getByText('First objective', { exact: true })).toHaveCount(0)
  await expect(nav.getByRole('button', { name: 'Next' })).toBeDisabled()
  expect(objectiveRequests).toEqual([null, objectiveCursor])
  await page.getByLabel('New company objective').fill('Created objective')
  await page.getByRole('button', { name: 'Add', exact: true }).click()
  await expect(page.getByText('Created objective', { exact: true })).toBeVisible()
  await expect(nav.getByRole('status')).toHaveText('Page 1')
  expect(objectiveRequests).toEqual([null, objectiveCursor, null])
})

test('changing period retires a held continuation and starts the chosen period at page one', async ({ page }) => {
  await prepareDashboardPages(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/okr/objectives/page?**', async (route) => {
    if (!new URL(route.request().url()).searchParams.has('cursor')) return route.fallback()
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { items: [objective('Retired objective')], next_cursor: null } })
  })
  try {
    await page.goto('/app/okr')
    await expect(page.getByText('First objective', { exact: true })).toBeVisible()
    await page.getByRole('navigation', { name: 'Objective pages' }).getByRole('button', { name: 'Next' }).click()
    await requested.promise
    await page.getByLabel('Period').selectOption(previousPeriod.start)
    await expect(page.getByText('Previous objective', { exact: true })).toBeVisible()
    release.resolve()
    await expect(page.getByRole('navigation', { name: 'Objective pages' }).getByRole('status')).toHaveText('Page 1')
    await expect(page.getByText('Retired objective', { exact: true })).toHaveCount(0)
    await page.getByLabel('Period').selectOption(currentPeriod.start)
    await expect(page.getByText('First objective', { exact: true })).toBeVisible()
  } finally { release.resolve() }
})

test('relationship add preserves unseen rows and delete restarts the visible page', async ({ page }) => {
  const { relationshipRequests } = await prepareDashboardPages(page)
  await page.goto(`/app/agents/${originalAgent.id}/relationships`)
  await expect(page.getByText('First', { exact: true })).toBeVisible()
  expect(relationshipRequests).toEqual([null])
  const nav = page.getByRole('navigation', { name: 'Relationship pages' })
  await nav.getByRole('button', { name: 'Next' }).click()
  await expect(page.getByText('Unseen', { exact: true })).toBeVisible()
  await page.getByLabel('Choose a person').selectOption('Added')
  await page.getByRole('button', { name: 'Add', exact: true }).click()
  await expect(page.getByText('Relationship saved', { exact: true })).toBeVisible()
  await nav.getByRole('button', { name: 'Next' }).click()
  await expect(page.getByText('Unseen', { exact: true })).toBeVisible()
  await expect(page.getByRole('list', { name: 'Relationships', exact: true }).getByText('Added', { exact: true })).toBeVisible()
  await expect(page.getByText('First', { exact: true })).toHaveCount(0)
  await nav.getByRole('button', { name: 'Previous' }).click()
  await expect(page.getByText('First', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Remove', exact: true }).click()
  await expect(page.getByText('No relationships yet.')).toBeVisible()
  await expect(nav.getByRole('status')).toHaveText('Page 1')
  expect(relationshipRequests).toEqual([null, relationshipCursor, null, relationshipCursor, null])
})

test('changing agents retires a held relationship page and clears selection', async ({ page }) => {
  await prepareDashboardPages(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**/api/agents/${originalAgent.id}/relationships/page?**`, async (route) => {
    if (!new URL(route.request().url()).searchParams.has('cursor')) return route.fallback()
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { items: [relationship('Retired person')], next_cursor: null, has_more: false } })
  })
  try {
    await page.goto(`/app/agents/${originalAgent.id}/relationships`)
    await expect(page.getByText('First', { exact: true })).toBeVisible()
    await page.getByLabel('Choose a person').selectOption('Added')
    await page.getByRole('navigation', { name: 'Relationship pages' }).getByRole('button', { name: 'Next' }).click()
    await requested.promise
    await page.goto(`/app/agents/${replacementAgent.id}/relationships`)
    await expect(page.getByText('Replacement person', { exact: true })).toBeVisible()
    release.resolve()
    await expect(page.getByText('Retired person', { exact: true })).toHaveCount(0)
    await expect(page.getByLabel('Choose a person')).toHaveValue('')
    await expect(page.getByRole('navigation', { name: 'Relationship pages' }).getByRole('status')).toHaveText('Page 1')
  } finally { release.resolve() }
})
