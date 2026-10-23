import { expect, test } from '@playwright/test'

import { inboxPath, notice, prepareNotifications, replacementNotice } from './notifications-fixture'
import { replacementToken, writeFromOtherTab } from './session-fencing-fixture'

test('bulk mutation retains its submitted category while the selected category changes', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page, [notice('Tool', 'autonomy_l2'), notice('Approval', 'approval_pending')])
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/notifications/read-all?category=tool', async (route) => {
    requested.resolve()
    await release.promise
    await route.fallback()
  })
  await page.goto(inboxPath)
  const category = page.getByLabel('Category', { exact: true })
  await category.selectOption('tool')
  await page.getByRole('button', { name: 'Mark all read', exact: true }).click()
  await requested.promise
  // When
  await category.selectOption('approval')
  release.resolve()
  // Then
  await expect(page.getByText('1 unread in Approvals', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Mark as read: Approval', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: /^Inbox\s*,\s*1 unread$/ }).first()).toBeVisible()
  expect(fixture.requests.filter((request) => request.method === 'POST').map((request) => request.category)).toEqual(['tool'])
})

test('a failed next page retains loaded rows and retries the same offset', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page, Array.from({ length: 51 }, (_, index) => notice(`Tool ${index}`, 'autonomy_l2')))
  await page.goto(inboxPath)
  const list = page.getByRole('list', { name: 'Notifications' })
  await expect(list.getByRole('listitem')).toHaveCount(50)
  fixture.failLists(true)
  // When
  await page.getByRole('button', { name: 'Load more', exact: true }).click()
  // Then
  await expect(page.getByRole('alert')).toContainText('Unable to load more')
  await expect(list.getByRole('listitem')).toHaveCount(50)
  fixture.failLists(false)
  await page.getByRole('button', { name: 'Retry load more', exact: true }).click()
  await expect(list.getByRole('listitem')).toHaveCount(51)
  expect(fixture.requests.filter((request) => request.path === '/api/notifications').map((request) => request.offset)).toEqual([0, 50, 50, 50])
})

test('failed category counts expose a retry without enabling an unknown bulk scope', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page, [notice('Approval', 'approval_pending')])
  fixture.failCounts(true)
  await page.goto(inboxPath)
  await page.getByLabel('Category', { exact: true }).selectOption('approval')
  await expect(page.getByRole('button', { name: 'Retry unread count', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Mark all read', exact: true })).toBeDisabled()
  fixture.failCounts(false)
  // When
  await page.getByRole('button', { name: 'Retry unread count', exact: true }).click()
  // Then
  await expect(page.getByText('1 unread in Approvals', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Mark all read', exact: true })).toBeEnabled()
})

for (const status of [204, 503]) {
  test(`retired bulk response ${status} cannot change replacement inbox state`, async ({ page }) => {
    // Given
    await prepareNotifications(page, [notice('Original tool', 'autonomy_l2')])
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route('**/api/notifications/read-all?category=all', async (route) => {
      requested.resolve()
      await release.promise
      await route.fulfill(status === 204 ? { status } : { status, json: { detail: 'Obsolete write failed' } })
    })
    await page.goto(inboxPath)
    await page.getByRole('button', { name: 'Mark all read', exact: true }).click()
    await requested.promise
    await writeFromOtherTab(page, replacementToken)
    await expect(page.getByText(replacementNotice.title, { exact: true })).toBeVisible()
    const completed = page.waitForEvent('requestfinished', { predicate: (request) => request.method() === 'POST' && request.url().includes('/notifications/read-all') })
    // When
    release.resolve()
    await completed
    // Then
    await expect(page.getByRole('button', { name: `Mark as read: ${replacementNotice.title}`, exact: true })).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByRole('link', { name: /^Inbox\s*,\s*1 unread$/ }).first()).toBeVisible()
  })
}
