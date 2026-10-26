import { expect, test } from '@playwright/test'

import { NotificationKind } from '../src/lib/notifications-api'
import { inboxPath, notice, prepareNotifications, replacementNotice } from './notifications-fixture'
import { replacementToken, writeFromOtherTab } from './session-fencing-fixture'
import { registerSelectedNotificationTests } from './selected-notification-cases'

registerSelectedNotificationTests()

test('bulk mutation retains its submitted category while the selected category changes', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page, [notice('Tool', NotificationKind.AutonomyL2), notice('Approval', NotificationKind.ApprovalPending)])
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

test('a failed next page retains loaded rows and retries the same cursor', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page, Array.from({ length: 51 }, (_, index) => notice(`Tool ${index}`, NotificationKind.AutonomyL2)))
  await page.goto(inboxPath)
  const list = page.getByRole('list', { name: 'Notifications' })
  await expect(list.getByRole('listitem')).toHaveCount(50)
  fixture.failLists(true)
  // When
  const beforeLoadMore = fixture.requests.length
  await page.getByRole('button', { name: 'Load more', exact: true }).click()
  // Then
  await expect(page.getByRole('alert')).toContainText('Unable to load more')
  await expect(list.getByRole('listitem')).toHaveCount(50)
  fixture.failLists(false)
  await page.getByRole('button', { name: 'Retry load more', exact: true }).click()
  await expect(list.getByRole('listitem')).toHaveCount(51)
  expect(fixture.requests.slice(beforeLoadMore)
    .filter((request) => request.path === '/api/notifications/page' && request.cursor !== null)
    .map((request) => request.cursor)).toEqual(['Tool 49', 'Tool 49', 'Tool 49'])
})

test('failed category counts expose a retry without enabling an unknown bulk scope', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page, [notice('Approval', NotificationKind.ApprovalPending)])
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
    await prepareNotifications(page, [notice('Original tool', NotificationKind.AutonomyL2)])
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

test('a late Active next page cannot appear after switching to Archived', async ({ page }) => {
  // Given
  await prepareNotifications(page, [...Array.from({ length: 51 }, (_, index) => notice(`Active ${index}`, NotificationKind.AutonomyL2)), { ...notice('Archived tool', NotificationKind.AutonomyL2), archived_at: '2030-01-02T00:00:00Z' }])
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/notifications/page?category=tool&**&cursor=Active+49', async (route) => {
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { items: [notice('Obsolete Active page', NotificationKind.AutonomyL2)], next_cursor: null } })
  })
  try {
    await page.goto(inboxPath)
    await page.getByLabel('Category', { exact: true }).selectOption('tool')
    await page.getByRole('button', { name: 'Load more', exact: true }).click()
    await requested.promise
    // When
    await page.getByRole('button', { name: 'Archived', exact: true }).click()
    await expect(page.getByText('Archived tool', { exact: true })).toBeVisible()
    release.resolve()
    // Then
    await expect(page.getByRole('button', { name: 'Archived', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')).toHaveCount(1)
    await expect(page.getByText('Obsolete Active page', { exact: true })).toHaveCount(0)
  } finally {
    release.resolve()
  }
})

for (const action of ['archive', 'restore'] as const) {
  const label = action === 'archive' ? 'Archive' : 'Restore'
  test(`failed ${action} retains the row and count until a successful retry`, async ({ page }) => {
    // Given
    const fixture = await prepareNotifications(page, [{ ...notice('Move me', NotificationKind.Broadcast), archived_at: action === 'restore' ? '2030-01-02T00:00:00Z' : null }])
    await page.goto(inboxPath)
    if (action === 'restore') await page.getByRole('button', { name: 'Archived', exact: true }).click()
    fixture.failWrites(true)
    // When
    await page.getByRole('button', { name: `${label} notification: Move me`, exact: true }).click()
    // Then
    await expect(page.getByRole('alert')).toContainText('Unable to move this notification')
    await expect(page.getByRole('button', { name: 'Mark as read: Move me', exact: true })).toBeVisible()
    await expect(page.getByText(`${action === 'archive' ? 1 : 0} unread in All`, { exact: true })).toBeVisible()
    fixture.failWrites(false)
    await page.getByRole('button', { name: `${label} notification: Move me`, exact: true }).click()
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')).toHaveCount(0)
    await expect(page.getByText(`${action === 'archive' ? 0 : 1} unread in All`, { exact: true })).toBeVisible()
  })

  for (const status of [200, 503]) {
    test(`retired ${action} response ${status} cannot affect a replacement workspace`, async ({ page }) => {
      // Given
      const fixture = await prepareNotifications(page, [{ ...notice('Original move', NotificationKind.Broadcast), archived_at: action === 'restore' ? '2030-01-02T00:00:00Z' : null }])
      const requested = Promise.withResolvers<void>()
      const release = Promise.withResolvers<void>()
      await page.route(`**/api/notifications/Original%20move/${action}`, async (route) => {
        requested.resolve()
        await release.promise
        await route.fulfill({ status, json: status === 200 ? { ok: true } : { detail: 'Obsolete write failed' } })
      })
      try {
        await page.goto(inboxPath)
        if (action === 'restore') await page.getByRole('button', { name: 'Archived', exact: true }).click()
        await page.getByRole('button', { name: `${label} notification: Original move`, exact: true }).click()
        await requested.promise
        await writeFromOtherTab(page, replacementToken)
        await expect(page.getByText(replacementNotice.title, { exact: true })).toBeVisible()
        const before = fixture.requests.length
        const completed = page.waitForEvent('requestfinished', { predicate: (request) => request.method() === 'POST' && request.url().endsWith(`/${action}`) })
        // When
        release.resolve()
        await completed
        // Then
        await expect(page.getByRole('button', { name: `Archive notification: ${replacementNotice.title}`, exact: true })).toBeEnabled()
        await expect(page.getByRole('alert')).toHaveCount(0)
        await expect(page.getByRole('link', { name: /^Inbox\s*,\s*1 unread$/ }).first()).toBeVisible()
        expect(fixture.requests.slice(before)).toEqual([])
      } finally {
        release.resolve()
      }
    })
  }
}

test('archive completion refreshes all overlapping categories after the selected view changes', async ({ page }) => {
  // Given
  await prepareNotifications(page, [notice('Announcement', NotificationKind.Broadcast), notice('Tool', NotificationKind.AutonomyL2)])
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/notifications/Announcement/archive', async (route) => {
    requested.resolve()
    await release.promise
    await route.fallback()
  })
  try {
    await page.goto(inboxPath)
    const category = page.getByLabel('Category', { exact: true })
    await category.selectOption('social')
    await category.selectOption('broadcast')
    await page.getByRole('button', { name: 'Archive notification: Announcement', exact: true }).click()
    await requested.promise
    // When
    await page.getByRole('button', { name: 'Archived', exact: true }).click()
    await category.selectOption('social')
    release.resolve()
    // Then
    await expect(page.getByRole('button', { name: 'Restore notification: Announcement', exact: true })).toBeEnabled()
    await expect(page.getByText('0 unread in Social', { exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: /^Inbox\s*,\s*1 unread$/ }).first()).toBeVisible()
  } finally {
    release.resolve()
  }
})
