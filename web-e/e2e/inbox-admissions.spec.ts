import { expect, test } from '@playwright/test'

import { prepareHistory } from './admission-history-fixture'
import { originalUser } from './session-fencing-fixture'
import { admission, payment, prepareTransfers } from './company-transfers-fixture'
import { notice, prepareNotifications } from './notifications-fixture'

const admissionsInbox = '/app/notifications?category=admissions'

for (const workspaceAllowed of [false, true]) for (const [from, to] of [['all', 'admissions'], ['admissions', 'approval']]) {
  test(`Category keeps keyboard focus when switching from ${from} to ${to} with workspace access ${workspaceAllowed}`, async ({ page }) => {
    // Given
    if (workspaceAllowed) await prepareTransfers(page)
    else await prepareHistory(page)
    await page.route('**/api/notifications/page*', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
    await page.goto(`/app/notifications?category=${from}`)
    await expect(page.locator('aside nav').getByRole('link')).toHaveCount(workspaceAllowed ? 6 : 2)
    await expect(from === 'admissions' ? page.getByLabel('Request status') : page.getByRole('button', { name: 'Active', exact: true })).toBeVisible()
    const category = page.getByLabel('Category', { exact: true })
    await category.focus()
    await expect(category).toBeFocused()
    // When
    await category.selectOption(to)
    // Then
    await expect(category).toHaveValue(to)
    await expect(category).toBeFocused()
  })
}

for (const width of [390, 1440]) {
  for (const detail of ['admission', 'transfer'] as const) {
    test(`Inbox stays active and visible on ${detail} detail at ${width}`, async ({ page }) => {
      // Given
      await page.setViewportSize({ width, height: 900 })
      await prepareTransfers(page, { saved: payment, destination: admission })
      // When
      await page.goto(detail === 'admission' ? `/app/admissions/${admission.id}` : `/app/admissions/transfers/${payment.id}`)
      // Then
      await expect(page.getByRole('region', { name: detail === 'admission' ? 'Admission status' : 'Saved transfer policy' })).toBeVisible()
      const navigation = page.locator(width === 390 ? 'header nav' : 'aside nav')
      const inbox = navigation.getByRole('link', { name: /^Inbox/ })
      await expect(inbox).toHaveAttribute('aria-current', 'page')
      await expect(inbox).toHaveClass(/\btext-foreground\b/)
      await expect(inbox.locator('img')).toHaveAttribute('src', '/nav-icons/inbox.svg')
      await expect(inbox).toBeInViewport({ ratio: 1 })
      if (width === 390) {
        await expect.poll(() => navigation.evaluate((rail) => {
          const active = rail.querySelector('[aria-current="page"]')
          if (!active) return false
          const bounds = rail.getBoundingClientRect(), target = active.getBoundingClientRect()
          return target.left >= bounds.left && target.right <= bounds.right
        })).toBe(true)
      }
    })
  }
}

test('Inbox categories survive reload and browser history without becoming wire categories', async ({ page }) => {
  // Given
  await prepareHistory(page)
  const categories: string[] = []
  await page.route('**/api/notifications**', (route) => {
    const url = new URL(route.request().url())
    categories.push(url.searchParams.get('category') ?? 'all')
    return route.fulfill({ json: url.pathname.endsWith('/unread-count') ? { count: 0 } : { items: [], next_cursor: null } })
  })
  await page.goto('/app/notifications')
  // When
  await page.getByLabel('Category', { exact: true }).selectOption('admissions')
  // Then
  await expect(page).toHaveURL(admissionsInbox)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Inbox')
  await expect(page.getByLabel('Category', { exact: true })).toHaveCount(1)
  await expect(page.getByLabel('Request status')).toHaveValue('all')
  await expect(page.getByRole('list', { name: 'Admission requests' }).getByRole('listitem')).toHaveCount(2)
  await expect(page.getByRole('navigation', { name: 'Workspace' }).getByRole('link', { name: 'Admissions', exact: true })).toHaveCount(0)
  await page.reload()
  await expect(page.getByLabel('Category', { exact: true })).toHaveValue('admissions')
  await page.getByLabel('Category', { exact: true }).selectOption('approval')
  await expect(page).toHaveURL('/app/notifications?category=approval')
  await expect(page.getByRole('button', { name: 'Active', exact: true })).toBeVisible()
  await page.goBack()
  await expect(page.getByLabel('Category', { exact: true })).toHaveValue('admissions')
  await expect(page.getByLabel('Request status')).toHaveValue('all')
  await page.goForward()
  await expect(page.getByLabel('Category', { exact: true })).toHaveValue('approval')
  expect(categories).toContain('approval')
  expect(categories).not.toContain('admissions')
})

test('the legacy list resolves to Inbox Admissions without onboarding', async ({ page }) => {
  // Given
  await prepareHistory(page)
  let onboardingCalls = 0
  await page.route('**/api/onboarding/status', (route) => {
    onboardingCalls++
    return route.fulfill({ status: 503, json: { detail: 'Onboarding is unavailable' } })
  })
  // When
  await page.goto('/app/admissions')
  // Then
  await expect(page).toHaveURL(admissionsInbox)
  await expect(page.getByLabel('Request status')).toHaveValue('all')
  expect(onboardingCalls).toBe(0)
})

for (const mustChangePassword of [false, true]) {
  test(`missing-organization applicant ${mustChangePassword ? 'must change password' : 'can see history but cannot enter workspace'}`, async ({ page }) => {
    // Given
    await prepareHistory(page)
    await page.route('**/api/member/auth/me', (route) => route.fulfill({ json: { ...originalUser, tenant_id: null, must_change_password: mustChangePassword } }))
    // When
    await page.goto(admissionsInbox)
    // Then
    if (mustChangePassword) {
      await expect(page).toHaveURL('/app/settings')
      await expect(page.getByLabel('Current password', { exact: true })).toBeVisible()
      await expect(page.getByLabel('Request status')).toHaveCount(0)
    } else {
      await expect(page.getByLabel('Request status')).toHaveValue('all')
      await expect(page.getByRole('navigation', { name: 'Workspace' }).getByRole('link')).toHaveCount(2)
      await page.goto('/app/agents')
      await expect(page).toHaveURL('/join')
      await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toHaveCount(0)
    }
  })
}

test('Inbox transfer history opens existing detail and returns to its Admissions category', async ({ page }) => {
  // Given
  await prepareTransfers(page, { saved: payment })
  // When
  await page.goto(admissionsInbox)
  await page.getByRole('link', { name: /^View transfer request/ }).click()
  // Then
  await expect(page).toHaveURL(`/app/admissions/transfers/${payment.id}`)
  await expect(page.getByRole('region', { name: 'Saved transfer policy' })).toContainText(payment.instructions)
  await page.getByRole('link', { name: 'All admission requests' }).click()
  await expect(page).toHaveURL(admissionsInbox)
  await expect(page.getByRole('region', { name: 'Transfer requests' }).getByRole('listitem')).toHaveCount(1)
})

test('a selected read finishing after an Admissions visit preserves the new notification selection and focus', async ({ page }) => {
  // Given
  await prepareNotifications(page, [
    { ...notice('Original notice', 'broadcast'), id: '11111111-1111-4111-8111-111111111111' },
    { ...notice('New selection', 'autonomy_l2'), id: '22222222-2222-4222-8222-222222222222' },
  ])
  await page.route('**/api/company-admissions/**', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  const requested = Promise.withResolvers<void>(), release = Promise.withResolvers<void>()
  await page.route('**/api/notifications/read-selected', async (route) => {
    requested.resolve(); await release.promise; await route.fallback()
  })
  try {
    await page.goto('/app/notifications')
    await page.getByRole('checkbox', { name: 'Select notification: Original notice', exact: true }).check()
    await page.getByRole('button', { name: 'Mark selected read', exact: true }).click()
    await requested.promise
    // When
    await page.getByLabel('Category', { exact: true }).selectOption('admissions')
    await expect(page.getByLabel('Request status')).toBeVisible()
    await page.getByLabel('Category', { exact: true }).selectOption('tool')
    const newer = page.getByRole('checkbox', { name: 'Select notification: New selection', exact: true })
    await newer.check(); await newer.focus()
    const completed = page.waitForEvent('requestfinished', { predicate: (request) => request.url().endsWith('/read-selected') })
    release.resolve(); await completed
    // Then
    await expect(newer).toBeChecked()
    await expect(newer).toBeFocused()
    await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).not.toBeFocused()
  } finally { release.resolve() }
})
