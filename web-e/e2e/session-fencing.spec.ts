import { expect, test } from '@playwright/test'

import {
  deferCreation, delayedAgent, originalAgent, originalToken, prepareWorkspace, replacementAgent,
  replacementToken, replacementUser, switchFromSettings, tokenKey, writeFromOtherTab,
} from './session-fencing-fixture'

for (const switchToken of [replacementToken, originalToken]) {
  test(`settings switch replaces the primed agents cache (${switchToken === originalToken ? 'same' : 'new'} token)`, async ({ page }) => {
    // Given: navigating within the SPA preserves the old cache unless its owner changes.
    await prepareWorkspace(page, replacementUser, switchToken)
    await page.goto('/app/agents')
    await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()

    // When
    await switchFromSettings(page)

    // Then: wait for the new identity, not a toast or an arbitrary delay.
    await expect(page.getByText(`Signed in as ${replacementUser.display_name}.`, { exact: false })).toBeVisible()
    await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
    const cards = page.locator('main a[href^="/app/agents/"]:not([href="/app/agents/new"])')
    await expect(cards).toHaveCount(1)
    await expect(cards).toHaveAttribute('href', `/app/agents/${replacementAgent.id}/chat`)
    await expect(cards).toContainText(replacementAgent.name)
    await expect(page.getByText(originalAgent.name, { exact: true })).toHaveCount(0)
  })
}

for (const boundary of ['storage replacement', 'sign-out'] as const) {
  for (const outcome of ['success', 'error'] as const) {
    test(`late workspace mutation ${outcome} has no effects after ${boundary}`, async ({ page }) => {
      // Given: hold the real UI's creation request until the old owner is gone.
      await prepareWorkspace(page)
      const creation = await deferCreation(page, outcome)
      await page.goto('/app/agents/new')
      await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
      await page.getByRole('button', { name: 'Create', exact: true }).click()
      await creation.requested
      switch (boundary) {
        case 'storage replacement':
          await writeFromOtherTab(page, replacementToken)
          await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
          await expect(page.getByText(`Signed in as ${replacementUser.display_name}.`, { exact: false })).toBeVisible()
          break
        case 'sign-out':
          await page.getByRole('button', { name: 'Sign out', exact: true }).first().click()
          await expect(page).toHaveURL('/login')
          await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
          break
        default:
          boundary satisfies never
      }
      await page.clock.install({ time: new Date('2030-01-01T00:00:00Z') })
      await page.clock.pauseAt(new Date('2030-01-01T01:00:00Z'))
      const ownerUrl = page.url()
      const completed = page.waitForEvent('requestfinished', {
        predicate: (request) => request.method() === 'POST' && new URL(request.url()).pathname === '/api/agents/',
      })

      // When
      creation.release()

      // Then: completed transport plus virtual time drains mutation/toast timers, without sleep.
      await completed
      await page.clock.runFor(1_000)
      await expect.soft(page.locator('[data-sonner-toast]'), 'An obsolete mutation must never notify the current owner').toHaveCount(0)
      await expect.soft(page, 'An obsolete success must not navigate to its old-company agent').toHaveURL(ownerUrl)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(
        boundary === 'storage replacement' ? replacementToken : null,
      )
    })
  }
}

for (const outcome of ['success', 'error'] as const) {
  test(`workspace mutation ${outcome} still notifies its current owner`, async ({ page }) => {
    // Given: controls prove the same deferred responses really produce observable effects.
    await prepareWorkspace(page)
    const creation = await deferCreation(page, outcome)
    await page.goto('/app/agents/new')
    await page.getByLabel('Name', { exact: true }).fill(delayedAgent.name)
    await page.getByRole('button', { name: 'Create', exact: true }).click()
    await creation.requested

    // When
    creation.release()

    // Then
    await expect(page.locator(`[data-sonner-toast][data-type="${outcome}"]`)).toHaveCount(1)
    await expect(page).toHaveURL(outcome === 'success' ? `/app/agents/${delayedAgent.id}/chat` : '/app/agents/new')
  })
}

test('storage sign-out from another real browser tab removes the open workspace', async ({ page }) => {
  // Given
  await prepareWorkspace(page)
  await page.goto('/app/agents')
  await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()

  // When: a native storage event, not a synthetic event or a reload.
  await writeFromOtherTab(page, null)

  // Then
  await expect(page).toHaveURL('/login')
  await expect(page.getByRole('heading', { name: 'Agents', exact: true })).toHaveCount(0)
  await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
})

test('settings switch preserves the replacement identity must-change-password gate', async ({ page }) => {
  // Given
  await prepareWorkspace(page, { ...replacementUser, must_change_password: true })
  await page.goto('/app/agents')
  await expect(page.getByRole('link', { name: new RegExp(originalAgent.name) })).toBeVisible()

  // When
  await switchFromSettings(page)

  // Then: Settings stays accessible, while Agents still redirects to Account.
  await expect(page).toHaveURL('/app/settings')
  await expect(page.getByText(`Signed in as ${replacementUser.display_name}.`, { exact: false })).toBeVisible()
  await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
  await expect(page).toHaveURL('/app/account')
  await expect(page.getByLabel('Display name')).toHaveValue(replacementUser.display_name)
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
  await expect(page).toHaveURL('/app/settings')
})
