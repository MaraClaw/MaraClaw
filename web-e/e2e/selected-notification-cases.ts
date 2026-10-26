import { expect, test } from '@playwright/test'

import { inboxPath, notice, prepareNotifications, replacementNotice } from './notifications-fixture'
import { replacementToken, writeFromOtherTab } from './session-fencing-fixture'

const first = { ...notice('Announcement', 'broadcast'), id: '11111111-1111-4111-8111-111111111111' }
const second = { ...notice('Tool', 'autonomy_l2'), id: '22222222-2222-4222-8222-222222222222' }

export function registerSelectedNotificationTests() {
  test('selected batch snapshots IDs while filter navigation and a newer selection remain usable', async ({ page }) => {
    // Given
    const fixture = await prepareNotifications(page, [first, second])
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route('**/api/notifications/read-selected', async (route) => {
      expect(route.request().postDataJSON()).toEqual({ ids: [first.id] })
      requested.resolve()
      await release.promise
      await route.fallback()
    })
    try {
      await page.goto(inboxPath)
      await page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true }).check()
      await page.getByRole('button', { name: 'Mark selected read', exact: true }).click()
      await requested.promise
      await expect(page.getByRole('button', { name: 'Marking selected read…', exact: true })).toBeDisabled()
      await expect(page.getByRole('button', { name: 'Mark all read', exact: true })).toBeDisabled()
      await expect(page.getByRole('button', { name: 'Archive notification: Tool', exact: true })).toBeDisabled()
      // When
      await page.getByLabel('Category', { exact: true }).selectOption('tool')
      const newer = page.getByRole('checkbox', { name: 'Select notification: Tool', exact: true })
      await newer.check()
      release.resolve()
      // Then
      await expect(page.getByRole('button', { name: 'Mark selected read', exact: true })).toBeEnabled()
      await expect(newer).toBeChecked()
      await expect(page.getByText('1 unread in Tools', { exact: true })).toBeVisible()
      await expect(page.getByRole('link', { name: /^Inbox\s*,\s*1 unread$/ }).first()).toBeVisible()
      expect(fixture.requests.filter((request) => request.method === 'POST')).toHaveLength(1)
      await page.getByRole('button', { name: 'Archived', exact: true }).click()
      await expect(page.getByText('0 selected (up to 200 loaded notifications)', { exact: true })).toBeVisible()
    } finally { release.resolve() }
  })

  test('failed selected batch preserves selection for retry and refreshes overlapping categories on success', async ({ page }) => {
    // Given
    const fixture = await prepareNotifications(page, [first, second])
    await page.goto(inboxPath)
    await page.getByLabel('Category', { exact: true }).selectOption('social')
    const checkbox = page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true })
    await checkbox.check()
    fixture.failWrites(true)
    // When
    await page.getByRole('button', { name: 'Mark selected read', exact: true }).click()
    // Then
    await expect(page.getByRole('alert')).toContainText('Unable to mark notifications as read')
    await expect(checkbox).toBeChecked()
    await expect(page.getByText('1 unread in Social', { exact: true })).toBeVisible()
    fixture.failWrites(false)
    await page.getByRole('button', { name: 'Mark selected read', exact: true }).click()
    await expect(checkbox).not.toBeChecked()
    await page.getByLabel('Category', { exact: true }).selectOption('broadcast')
    await expect(page.getByText('0 unread in Broadcasts', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Mark as read: Announcement', exact: true })).toHaveCount(0)
  })

  test('load-more keeps selection after failure, clears it after success, and prunes archived rows', async ({ page }) => {
    // Given
    const fixture = await prepareNotifications(page, [first, ...Array.from({ length: 50 }, (_, index) => ({ ...second, id: `22222222-2222-4222-8222-${String(index).padStart(12, '0')}`, title: `Tool ${index}` }))])
    await page.goto(inboxPath)
    const checkbox = page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true })
    await checkbox.check()
    fixture.failLists(true)
    // When
    await page.getByRole('button', { name: 'Load more', exact: true }).click()
    // Then
    await expect(page.getByRole('alert')).toContainText('Unable to load more')
    await expect(checkbox).toBeChecked()
    fixture.failLists(false)
    await page.getByRole('button', { name: 'Retry load more', exact: true }).click()
    await expect(page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')).toHaveCount(51)
    await expect(checkbox).not.toBeChecked()
    await checkbox.check()
    await page.getByRole('button', { name: 'Archive notification: Announcement', exact: true }).click()
    await expect(checkbox).toHaveCount(0)
    await expect(page.getByText('0 selected (up to 200 loaded notifications)', { exact: true })).toBeVisible()
  })

  for (const status of [200, 503]) {
    test(`retired selected response ${status} preserves replacement selection and errors`, async ({ page }) => {
      // Given
      await prepareNotifications(page, [first])
      const requested = Promise.withResolvers<void>()
      const release = Promise.withResolvers<void>()
      await page.route('**/api/notifications/read-selected', async (route) => {
        requested.resolve()
        await release.promise
        await route.fulfill({ status, json: status === 200 ? { ok: true } : { detail: 'Obsolete failure' } })
      })
      try {
        await page.goto(inboxPath)
        await page.getByRole('checkbox', { name: 'Select notification: Announcement', exact: true }).check()
        await page.getByRole('button', { name: 'Mark selected read', exact: true }).click()
        await requested.promise
        await writeFromOtherTab(page, replacementToken)
        const replacement = page.getByRole('checkbox', { name: `Select notification: ${replacementNotice.title}`, exact: true })
        await replacement.check()
        const completed = page.waitForEvent('requestfinished', { predicate: (request) => request.url().endsWith('/read-selected') })
        // When
        release.resolve()
        await completed
        // Then
        await expect(replacement).toBeChecked()
        await expect(page.getByRole('button', { name: 'Mark selected read', exact: true })).toBeEnabled()
        await expect(page.getByRole('alert')).toHaveCount(0)
      } finally { release.resolve() }
    })
  }

  test('selection is limited to 200 rendered loaded rows', async ({ page }) => {
    // Given
    const fixture = await prepareNotifications(page, Array.from({ length: 201 }, (_, index) => ({ ...second, id: `22222222-2222-4222-8222-${String(index).padStart(12, '0')}`, title: `Tool ${index}` })))
    await page.goto(inboxPath)
    for (let index = 0; index < 4; index++) await page.getByRole('button', { name: 'Load more', exact: true }).click()
    await expect(page.getByRole('checkbox')).toHaveCount(201)
    // When
    await page.getByRole('checkbox').evaluateAll((elements) => { for (const element of elements) if (element instanceof HTMLInputElement) element.click() })
    // Then
    await expect(page.getByRole('checkbox', { checked: true })).toHaveCount(200)
    await expect(page.getByRole('checkbox').last()).toBeDisabled()
    expect(fixture.requests.filter((request) => request.method === 'POST')).toHaveLength(0)
  })
}
