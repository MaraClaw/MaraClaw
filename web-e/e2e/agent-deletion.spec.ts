import { expect, test } from '@playwright/test'

import { deferDeletion, deletionRequest, prepareDeletion, settingsPath } from './agent-deletion-fixture'
import { originalAgent, replacementUser, switchFromSettings } from './session-fencing-fixture'

test('request dialog restores focus after Escape without submitting', async ({ page }) => {
  // Given
  const api = await prepareDeletion(page)
  await page.goto(settingsPath)
  const trigger = page.getByRole('button', { name: 'Request deletion', exact: true })
  await trigger.click()
  await expect(page.getByRole('dialog', { name: 'Request deletion' })).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Reason', exact: true })).toBeFocused()
  // When
  await page.keyboard.press('Escape')
  // Then
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(trigger).toBeFocused()
  expect(api.posts).toEqual([])
})

for (const reason of [' \n\t ', 'x'.repeat(1001)]) {
  test(`invalid ${reason.length}-character reason stays in the dialog without submitting`, async ({ page }) => {
    // Given
    const api = await prepareDeletion(page)
    await page.goto(settingsPath)
    await page.getByRole('button', { name: 'Request deletion', exact: true }).click()
    const dialog = page.getByRole('dialog')
    const input = dialog.getByRole('textbox', { name: 'Reason', exact: true })
    await input.fill(reason)
    // When
    await dialog.getByRole('button', { name: 'Request deletion', exact: true }).click()
    // Then
    await expect(input).toHaveAttribute('aria-invalid', 'true')
    await expect(input).toBeFocused()
    await expect(dialog.getByRole('alert')).toBeVisible()
    expect(api.posts).toEqual([])
  })
}

test('valid reason submits a request, stays in Settings and prevents duplicate requests', async ({ page }) => {
  // Given
  const api = await prepareDeletion(page)
  await page.goto(settingsPath)
  const trigger = page.getByRole('button', { name: 'Request deletion', exact: true })
  await trigger.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('two distinct organization admins or the genesis admin')
  await dialog.getByRole('textbox', { name: 'Reason', exact: true }).fill('  x  ')
  // When
  await dialog.getByRole('button', { name: 'Request deletion', exact: true }).click()
  // Then
  await expect(dialog).toHaveCount(0)
  await expect(trigger).toBeFocused()
  await expect(trigger).toBeDisabled()
  await expect(page.getByRole('status').filter({ hasText: 'Deletion request pending' })).toContainText('1 of 2')
  await expect(page).toHaveURL(settingsPath)
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue(originalAgent.name)
  expect(api.posts).toEqual([{ agent_id: originalAgent.id, reason: 'x' }])
})

test('persisted pending collection blocks a second request on page load', async ({ page }) => {
  // Given
  const api = await prepareDeletion(page, true)
  // When
  await page.goto(settingsPath)
  // Then
  await expect(page.getByRole('button', { name: 'Request deletion', exact: true })).toBeDisabled()
  await expect(page.getByRole('status').filter({ hasText: 'Deletion request pending' })).toContainText('1 of 2')
  expect(api.posts).toEqual([])
})

test('duplicate conflict loads the persisted request instead of submitting again', async ({ page }) => {
  // Given
  const api = await prepareDeletion(page)
  let posts = 0
  await page.route('**/api/agent-deletion-requests', (route) => {
    posts++
    return route.fulfill({ status: 409, json: { detail: { code: 'deletion_request_pending', request_id: deletionRequest.id } } })
  })
  await page.goto(settingsPath)
  await page.getByRole('button', { name: 'Request deletion', exact: true }).click()
  await page.getByRole('textbox', { name: 'Reason', exact: true }).fill('Reason')
  // When
  await page.getByRole('dialog').getByRole('button', { name: 'Request deletion', exact: true }).click()
  // Then
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('status').filter({ hasText: 'Deletion request pending' })).toContainText('1 of 2')
  await expect(page.getByRole('button', { name: 'Request deletion', exact: true })).toBeDisabled()
  expect(posts).toBe(1)
  expect(api.detailReads).toHaveLength(1)
})

test('server failure preserves the reason and announces a recoverable error', async ({ page }) => {
  // Given
  await prepareDeletion(page)
  const request = await deferDeletion(page)
  try {
    await page.goto(settingsPath)
    await page.getByRole('button', { name: 'Request deletion', exact: true }).click()
    await page.getByRole('textbox', { name: 'Reason', exact: true }).fill('Keep this reason')
    await page.getByRole('dialog').getByRole('button', { name: 'Request deletion', exact: true }).click()
    await request.requested
    await expect(page.getByRole('button', { name: 'Submitting…', exact: true })).toBeDisabled()
    // When
    request.release(503)
    // Then
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('try again')
    await expect(page.getByRole('textbox', { name: 'Reason', exact: true })).toHaveValue('Keep this reason')
    await expect(page.getByRole('dialog').getByRole('button', { name: 'Request deletion', exact: true })).toBeEnabled()
  } finally { request.release(503) }
})

for (const retirement of ['logout', 'tenant-switch'] as const) {
  test(`late request completion cannot affect the workspace after ${retirement}`, async ({ page }) => {
    // Given
    await prepareDeletion(page)
    const request = await deferDeletion(page)
    try {
      await page.goto(settingsPath)
      await page.getByRole('button', { name: 'Request deletion', exact: true }).click()
      await page.getByRole('textbox', { name: 'Reason', exact: true }).fill('Reason')
      await page.getByRole('dialog').getByRole('button', { name: 'Request deletion', exact: true }).click()
      await request.requested
      await page.keyboard.press('Escape')
      if (retirement === 'logout') {
        await page.getByRole('button', { name: 'Sign out', exact: true }).first().click()
        await expect(page).toHaveURL('/login')
      } else {
        await switchFromSettings(page)
        await expect(page.getByLabel('Display name', { exact: true })).toHaveValue(replacementUser.display_name)
      }
      const completed = page.waitForEvent('requestfinished', { predicate: (transport) => transport.method() === 'POST' && new URL(transport.url()).pathname === '/api/agent-deletion-requests' })
      // When
      request.release()
      await completed
      // Then
      await expect(page.getByRole('status').filter({ hasText: 'Deletion request pending' })).toHaveCount(0)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      await expect(page).toHaveURL(retirement === 'logout' ? '/login' : '/app/settings')
    } finally { request.release() }
  })
}
