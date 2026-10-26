import { expect, test } from '@playwright/test'

import { NotificationKind } from '../src/lib/notifications-api'
import { inboxPath, notice, prepareNotifications, replacementNotice } from './notifications-fixture'
import { replacementToken, writeFromOtherTab } from './session-fencing-fixture'

test('category switching during a request never displays another category’s late rows', async ({ page }) => {
  // Given
  await prepareNotifications(page, [notice('Tool event', NotificationKind.AutonomyL2), notice('Approval event', NotificationKind.ApprovalPending)])
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/notifications/page?category=tool&**', async (route) => {
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { items: [notice('Late tool event', NotificationKind.AutonomyL2)], next_cursor: null } })
  })
  await page.goto(inboxPath)
  const category = page.getByLabel('Category', { exact: true })
  await expect(category.getByRole('option')).toHaveText(['All', 'Tools', 'Approvals', 'Social', 'Broadcasts'])
  await category.focus()
  await expect(category).toBeFocused()
  // When
  await category.selectOption('tool')
  await requested.promise
  await expect(page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')).toHaveCount(0)
  await category.selectOption('approval')
  await expect(page.getByText('Approval event', { exact: true })).toBeVisible()
  release.resolve()
  // Then
  await expect(page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')).toHaveCount(1)
  await expect(page.getByText('Late tool event', { exact: true })).toHaveCount(0)
})

test('load more uses cursors beyond 200 and retains tied rows across an interleaved insert', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page, Array.from({ length: 251 }, (_, index) => notice(`Tool ${index}`, NotificationKind.AutonomyL2, index < 250)))
  await page.goto(inboxPath)
  await page.getByLabel('Category', { exact: true }).selectOption('tool')
  const list = page.getByRole('list', { name: 'Notifications' })
  await expect(list.getByRole('listitem')).toHaveCount(50)
  fixture.insert(notice('New arrival', NotificationKind.AutonomyL2))
  // When
  for (const count of [100, 150, 200, 250, 251]) {
    await page.getByRole('button', { name: 'Load more', exact: true }).click()
    await expect(list.getByRole('listitem')).toHaveCount(count)
  }
  // Then
  await expect(list.getByText('Tool 0', { exact: true })).toBeVisible()
  await expect(list.getByText('Tool 250', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Load more', exact: true })).toHaveCount(0)
  expect(fixture.requests.filter((request) => request.category === 'tool' && request.path === '/api/notifications/page').map((request) => request.cursor)).toEqual([null, 'Tool 49', 'Tool 99', 'Tool 149', 'Tool 199', 'Tool 249'])
  await expect(list.getByText('New arrival', { exact: true })).toHaveCount(0)
})

test('bulk read includes unread rows beyond the page and preserves unrelated badge counts', async ({ page }) => {
  // Given
  await prepareNotifications(page, [
    ...Array.from({ length: 51 }, (_, index) => notice(`Tool ${index}`, NotificationKind.AutonomyL2, index < 50)),
    notice('Pending approval', NotificationKind.ApprovalPending), notice('Company broadcast', NotificationKind.Broadcast),
  ])
  await page.goto(inboxPath)
  await page.getByLabel('Category', { exact: true }).selectOption('tool')
  const bulk = page.getByRole('button', { name: 'Mark all read', exact: true })
  await expect(page.getByRole('button', { name: /^Mark as read:/ })).toHaveCount(0)
  await expect(bulk).toBeEnabled()
  await expect(bulk).toHaveAccessibleDescription(/ALL active notifications matching this category, including those not loaded/)
  // When
  await bulk.click()
  // Then
  await expect(page.getByText('0 unread in Tools', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: /^Inbox\s*,\s*2 unread$/ }).first()).toBeVisible()
  await expect(bulk).toBeDisabled()
})

test('broadcast bulk read refreshes both social and broadcast views', async ({ page }) => {
  // Given
  await prepareNotifications(page, [notice('Announcement', NotificationKind.Broadcast), notice('Comment', NotificationKind.PlazaComment), notice('Approval', NotificationKind.ApprovalResolved)])
  await page.goto(inboxPath)
  const category = page.getByLabel('Category', { exact: true })
  await category.selectOption('social')
  await expect(page.getByText('2 unread in Social', { exact: true })).toBeVisible()
  await category.selectOption('broadcast')
  // When
  await page.getByRole('button', { name: 'Mark all read', exact: true }).click()
  // Then
  await expect(page.getByText('0 unread in Broadcasts', { exact: true })).toBeVisible()
  await category.selectOption('social')
  await expect(page.getByText('1 unread in Social', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Mark as read: Announcement', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Mark as read: Comment', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: /^Inbox\s*,\s*2 unread$/ }).first()).toBeVisible()
})

test('failed list requests can retry to an empty category', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page)
  fixture.failLists(true)
  await page.goto(inboxPath)
  await expect(page.getByRole('alert')).toContainText('Unable to load notifications')
  fixture.failLists(false)
  // When
  await page.getByRole('button', { name: 'Retry', exact: true }).click()
  // Then
  await expect(page.getByText('No active notifications yet.', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Refresh Inbox', exact: true })).toBeEnabled()
})

test('failed bulk requests preserve unread rows and badge until a successful retry', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page, [notice('Unread tool', NotificationKind.AutonomyL2), notice('Unread approval', NotificationKind.ApprovalPending)])
  fixture.failWrites(true)
  await page.goto(inboxPath)
  await page.getByLabel('Category', { exact: true }).selectOption('tool')
  // When
  await page.getByRole('button', { name: 'Mark all read', exact: true }).click()
  // Then
  await expect(page.getByRole('alert')).toContainText('Unable to mark notifications')
  await expect(page.getByRole('button', { name: 'Mark as read: Unread tool', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: /^Inbox\s*,\s*2 unread$/ }).first()).toBeVisible()
  fixture.failWrites(false)
  await page.getByRole('button', { name: 'Mark all read', exact: true }).click()
  await expect(page.getByText('0 unread in Tools', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: /^Inbox\s*,\s*1 unread$/ }).first()).toBeVisible()
})

test('notification links keep Plaza remapping and existing member destinations', async ({ page }) => {
  // Given
  await prepareNotifications(page, [
    { ...notice('Plaza', NotificationKind.Mention), link: '/plaza?post=one#reply' },
    { ...notice('Admission', NotificationKind.CompanyAdmissionResolved), link: '/app/admissions/request-a' },
    { ...notice('Agent', NotificationKind.AutonomyL2), link: '/app/agents/agent-a/chat' },
    { ...notice('Update', NotificationKind.OpenClawUpdate), link: '/app/updates/update-a' },
  ])
  // When
  await page.goto(inboxPath)
  // Then
  for (const [title, href] of [['Plaza', '/app/plaza?post=one#reply'], ['Admission', '/app/admissions/request-a'], ['Agent', '/app/agents/agent-a/chat'], ['Update', '/app/updates/update-a']]) {
    await expect(page.getByRole('link', { name: `Open notification: ${title}`, exact: true })).toHaveAttribute('href', href)
  }
})

test('late paginated rows cannot cross a retired session boundary', async ({ page }) => {
  // Given
  await prepareNotifications(page, Array.from({ length: 51 }, (_, index) => notice(`Original ${index}`, NotificationKind.AutonomyL2)))
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/notifications/page?**&cursor=Original+49', async (route) => {
    requested.resolve()
    await release.promise
    await route.fulfill({ json: { items: [notice('Obsolete next page', NotificationKind.AutonomyL2)], next_cursor: null } })
  })
  await page.goto(inboxPath)
  await page.getByRole('button', { name: 'Load more', exact: true }).click()
  await requested.promise
  await writeFromOtherTab(page, replacementToken)
  await expect(page.getByText(replacementNotice.title, { exact: true })).toBeVisible()
  // When
  release.resolve()
  // Then
  await expect(page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')).toHaveCount(1)
  await expect(page.getByText('Obsolete next page', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('link', { name: /^Inbox\s*,\s*1 unread$/ }).first()).toBeVisible()
})

test('archive preserves unread state and links while Active bulk read excludes Archived', async ({ page }) => {
  // Given
  await prepareNotifications(page, [{ ...notice('Announcement', NotificationKind.Broadcast), link: '/plaza?post=one' }, notice('Tool', NotificationKind.AutonomyL2)])
  await page.goto(inboxPath)
  const category = page.getByLabel('Category', { exact: true })
  await category.selectOption('broadcast')
  // When
  await page.getByRole('button', { name: 'Archive notification: Announcement', exact: true }).click()
  // Then
  await expect(page.getByText('No active notifications yet.', { exact: false })).toBeVisible()
  await expect(page.getByRole('link', { name: /^Inbox\s*,\s*1 unread$/ }).first()).toBeVisible()
  await page.getByRole('button', { name: 'Archived', exact: true }).click()
  await expect(category).toHaveValue('broadcast')
  await expect(page.getByRole('button', { name: 'Mark all read', exact: true })).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Open notification: Announcement', exact: true })).toHaveAttribute('href', '/app/plaza?post=one')
  await expect(page.getByRole('button', { name: 'Mark as read: Announcement', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Active', exact: true }).click()
  await category.selectOption('all')
  await page.getByRole('button', { name: 'Mark all read', exact: true }).click()
  await expect(page.getByText('0 unread in All', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Archived', exact: true }).click()
  await page.getByRole('button', { name: 'Restore notification: Announcement', exact: true }).click()
  await expect(page.getByText('1 unread in All', { exact: true })).toBeVisible()
  await expect(page.getByText('No archived notifications', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: 'Active', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Mark as read: Announcement', exact: true })).toBeVisible()
  await category.selectOption('social')
  await expect(page.getByText('1 unread in Social', { exact: true })).toBeVisible()
})

test('view switches retain category and restart both previously loaded page sequences', async ({ page }) => {
  // Given
  const fixture = await prepareNotifications(page, ['active', 'archived'].flatMap((view) => Array.from({ length: 51 }, (_, index) => ({ ...notice(`${view} ${index}`, NotificationKind.AutonomyL2), archived_at: view === 'archived' ? '2030-01-02T00:00:00Z' : null }))))
  await page.goto(inboxPath)
  const category = page.getByLabel('Category', { exact: true })
  await category.selectOption('tool')
  const rows = page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')
  await page.getByRole('button', { name: 'Load more', exact: true }).click()
  await expect(rows).toHaveCount(51)
  await page.getByRole('button', { name: 'Archived', exact: true }).click()
  await page.getByRole('button', { name: 'Load more', exact: true }).click()
  await expect(rows).toHaveCount(51)
  // When
  await page.getByRole('button', { name: 'Active', exact: true }).click()
  // Then
  await expect(category).toHaveValue('tool')
  await expect(rows).toHaveCount(50)
  await expect(rows.first()).toContainText('active 0')
  await page.getByRole('button', { name: 'Archived', exact: true }).click()
  await expect(rows).toHaveCount(50)
  await expect(rows.first()).toContainText('archived 0')
  expect(fixture.requests.filter((request) => request.path === '/api/notifications/page' && request.category === 'tool').map(({ view, cursor }) => [view, cursor])).toEqual([['active', null], ['active', 'active 49'], ['archived', null], ['archived', 'archived 49'], ['active', null], ['archived', null]])
})

for (const width of [390, 1440, 1920]) {
  for (const colorScheme of ['light', 'dark'] as const) {
    test.describe(`${width} ${colorScheme} inbox backgrounds`, () => {
      test.use({ viewport: { width, height: width === 390 ? 844 : 900 }, isMobile: width === 390,
        hasTouch: width === 390, deviceScaleFactor: width === 390 ? 3 : 1, colorScheme, reducedMotion: 'reduce' })

      for (const readMode of ['one', 'all'] as const) {
        test(`backgrounds converge when marking ${readMode} read`, async ({ page }, testInfo) => {
          // Given mixed read states with the existing labels and destinations.
          await prepareNotifications(page, [
            { ...notice('company-announcement', NotificationKind.Broadcast), title: 'Company announcement', body: 'Review the latest company news and upcoming team plans.', link: '/plaza?post=announcement' },
            { ...notice('tool-request', NotificationKind.AutonomyL2), title: 'Tool request ready', body: 'Your requested tool is available for the next task.' },
            { ...notice('membership-approved', NotificationKind.CompanyAdmissionResolved, true), title: 'Membership approved', body: 'Your membership request has been approved.', link: '/app/admissions/request-a' },
          ])
          await page.clock.install({ time: new Date('2030-01-02T12:00:00Z') })
          await page.goto(inboxPath)
          const rows = page.getByRole('list', { name: 'Notifications' }).getByRole('listitem')
          await expect(rows).toHaveCount(3)
          const readBackground = await rows.nth(2).evaluate((row) => getComputedStyle(row).backgroundColor)
          const unreadBackground = await rows.first().evaluate((row) => getComputedStyle(row).backgroundColor)
          expect(unreadBackground).not.toBe(readBackground)
          await expect(rows.nth(1)).toHaveCSS('background-color', unreadBackground)
          await expect(rows.nth(2).getByText('Read', { exact: true })).toBeVisible()
          await page.screenshot({ path: testInfo.outputPath('mixed.png'), fullPage: true })
          if (width === 390) {
            await rows.last().scrollIntoViewIfNeeded()
            await page.screenshot({ path: testInfo.outputPath('mixed-end.png'), fullPage: true })
          }

          // When
          await page.getByRole('button', { name: readMode === 'one' ? 'Mark as read: Company announcement' : 'Mark all read', exact: true }).click()

          // Then the changed rows match the neutral read surface, not just their labels.
          await expect(rows.first()).toHaveCSS('background-color', readBackground)
          await expect(rows.first().getByText('Read', { exact: true })).toBeVisible()
          await expect(rows.nth(1)).toHaveCSS('background-color', readMode === 'one' ? unreadBackground : readBackground)
          await expect(rows.nth(2)).toHaveCSS('background-color', readBackground)
          await expect(page.getByRole('button', { name: /^Mark as read:/ })).toHaveCount(readMode === 'one' ? 1 : 0)
          await expect(page.getByText(`${readMode === 'one' ? 1 : 0} unread in All`, { exact: true })).toBeVisible()
          await page.getByRole('heading', { name: 'Inbox', exact: true }).scrollIntoViewIfNeeded()
          await page.screenshot({ path: testInfo.outputPath(`${readMode}-read.png`), fullPage: true })
          if (width === 390) {
            await rows.last().scrollIntoViewIfNeeded()
            await page.screenshot({ path: testInfo.outputPath(`${readMode}-read-end.png`), fullPage: true })
          }
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
          if (readMode === 'one') {
            await page.getByRole('button', { name: 'Archive notification: Tool request ready', exact: true }).click()
            await page.getByRole('button', { name: 'Archived', exact: true }).click()
            await expect(rows).toHaveCount(1)
            await expect(page.getByRole('button', { name: 'Restore notification: Tool request ready', exact: true })).toBeEnabled()
            await page.screenshot({ path: testInfo.outputPath('archived.png'), fullPage: true })
            expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
          }
        })
      }
    })
  }
}
