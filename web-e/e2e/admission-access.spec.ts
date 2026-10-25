import { expect, test } from '@playwright/test'

import { accessEndpoint, member, pending, prepareAccess, restrictedLabels, target, tokenKey, workspaceLabels, workspaceRequests } from './admission-access-fixture'

for (const width of [390, 1440]) {
  test(`persisted pending registration exposes only three destinations after reload at ${width}px`, async ({ page }, testInfo) => {
    // Given
    const { calls } = await prepareAccess(page, pending, false)
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/register')
    await page.getByLabel('Email', { exact: true }).fill(member.email ?? '')
    await page.getByLabel('Password', { exact: true }).fill('password')
    await page.getByLabel('Confirm password', { exact: true }).fill('password')
    await page.getByLabel('Invitation code', { exact: false }).fill('invite')
    // When
    await page.getByRole('button', { name: 'Create account', exact: true }).click()
    await expect(page).toHaveURL(`/app/admissions/${pending.id}`)
    await page.reload()
    // Then
    await expect(page.getByText('Awaiting review', { exact: true })).toBeVisible()
    const nav = page.getByRole('navigation', { name: 'Workspace', exact: true })
    await expect(nav.getByRole('link')).toHaveText(restrictedLabels)
    await expect(nav.getByRole('link')).toHaveCount(3)
    await page.screenshot({ path: testInfo.outputPath(`restricted-${width}.png`), fullPage: true })
    if (width === 390) await expect(page.getByRole('link', { name: 'MaraClaw Workspace', exact: true })).toHaveAttribute('href', '/app/notifications')
    await nav.getByRole('link', { name: 'Inbox', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).toBeVisible()
    await nav.getByRole('link', { name: 'Admissions', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Organization admissions', exact: true })).toBeVisible()
    await nav.getByRole('link', { name: 'Settings', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
    expect(workspaceRequests(calls)).toEqual([])
  })
}

for (const path of ['/app', '/app/agents', '/app/agents/one/settings', '/app/onboarding', '/app/plaza', '/app/okr', '/app/directory']) {
  test(`pending non-transfer applicant cannot bypass access using ${path}`, async ({ page }) => {
    // Given
    const { calls } = await prepareAccess(page)
    // When
    await page.goto(path)
    // Then
    await expect(page).toHaveURL('/app/notifications')
    await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).toBeVisible()
    expect(workspaceRequests(calls)).toEqual([])
  })
}

test('a delayed pending check never mounts blocked workspace queries', async ({ page }) => {
  // Given
  const { calls } = await prepareAccess(page)
  let release = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await page.route(accessEndpoint, async (route) => {
    await gate
    await route.fulfill({ json: { items: [pending], next_cursor: null } })
  })
  // When
  await page.goto('/app/agents/one/settings')
  // Then
  try {
    await expect(page.getByRole('status')).toHaveText('Checking organization access…')
    await expect(page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('link')).toHaveText(restrictedLabels)
    expect(workspaceRequests(calls)).toEqual([])
    release()
    await expect(page).toHaveURL('/app/notifications')
    await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).toBeVisible()
    expect(workspaceRequests(calls)).toEqual([])
  } finally { release() }
})

test('failed access checks fail closed and retry without mounting blocked queries', async ({ page }) => {
  // Given
  const { calls } = await prepareAccess(page)
  await page.route(accessEndpoint, (route) => route.fulfill({ status: 503, json: { detail: 'Unavailable' } }))
  await page.goto('/app/agents')
  await expect(page.getByRole('alert')).toContainText('Unable to confirm organization access')
  await expect(page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('link')).toHaveText(restrictedLabels)
  expect(workspaceRequests(calls)).toEqual([])
  await page.unroute(accessEndpoint)
  // When
  await page.getByRole('button', { name: 'Retry access check', exact: true }).click()
  // Then
  await expect(page).toHaveURL('/app/notifications')
  await expect(page.getByRole('heading', { name: 'Inbox', exact: true })).toBeVisible()
  expect(workspaceRequests(calls)).toEqual([])
})

for (const destination of [
  { path: '/app/notifications', heading: 'Inbox' },
  { path: '/app/admissions', heading: 'Organization admissions' },
  { path: '/app/account', heading: 'Settings' },
]) {
  test(`access-check failure leaves ${destination.path} available`, async ({ page }) => {
    // Given
    const { calls } = await prepareAccess(page)
    await page.route(accessEndpoint, (route) => new URL(route.request().url()).searchParams.get('limit') === '100'
      ? route.fulfill({ status: 503, json: { detail: 'Unavailable' } }) : route.fallback())
    // When
    await page.goto(destination.path)
    // Then
    await expect(page.getByRole('heading', { name: destination.heading, exact: true })).toBeVisible()
    expect(workspaceRequests(calls)).toEqual([])
  })
}

test('approved target session replaces the pending cache and restores seven workspace destinations', async ({ page }) => {
  // Given
  const fixture = await prepareAccess(page)
  await page.goto(`/app/admissions/${pending.id}`)
  await expect(page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('link')).toHaveText(restrictedLabels)
  fixture.setAdmission({ ...pending, status: 'approved', decided_at: '2026-10-25T13:00:00Z' })
  await page.getByRole('button', { name: 'Refresh status', exact: true }).click()
  // When
  await page.getByRole('button', { name: `Open ${target.name}`, exact: true }).click()
  // Then
  await expect(page).toHaveURL('/app/agents')
  await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('link')).toHaveText(workspaceLabels)
  expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe('target-token')
  expect(workspaceRequests(fixture.calls)).toContain('/api/agents/')
})

test('explicit pending transfers retain the source workspace', async ({ page }) => {
  // Given
  const { calls } = await prepareAccess(page, { ...pending, transfer: {
    transfer_request_id: '44444444-4444-4444-8444-444444444444', choice: 'paid',
    fee_amount_minor: 1000, currency: 'USD', free_consent_at: null,
  } })
  // When
  await page.goto('/app/agents')
  // Then
  await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toBeVisible()
  await expect(page).toHaveURL('/app/agents')
  await expect(page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('link')).toHaveText(workspaceLabels)
  expect(workspaceRequests(calls)).toContain('/api/agents/')
})

test('restricted access polls persisted admission status after thirty seconds', async ({ page }) => {
  // Given
  await page.clock.install()
  const fixture = await prepareAccess(page)
  await page.goto('/app/notifications')
  const links = page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('link')
  await expect(links).toHaveText(restrictedLabels)
  fixture.setAdmission({ ...pending, status: 'approved', decided_at: '2026-10-25T13:00:00Z' })
  // When
  await page.clock.fastForward(30_000)
  // Then
  await expect(links).toHaveText(workspaceLabels)
})
