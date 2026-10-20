import { expect, test } from '@playwright/test'

import { directoryPath, localDepartment, localMembers, prepareLocalDepartments, siblingDepartment } from './local-departments-fixture'
import {
  originalToken, replacementCompany, replacementToken, switchFromSettings, tokenKey, writeFromOtherTab,
} from './session-fencing-fixture'

const rosterPath = `/api/departments/${localDepartment.id}/members`
const siblingRosterPath = `/api/departments/${siblingDepartment.id}/members`

export function registerLocalDepartmentRosterTests() {
  test('keyboard disclosure lazily loads only its own roster and preserves duplicate and unnamed members privately', async ({ page }) => {
    // Given
    const state = await prepareLocalDepartments(page)
    const consoleErrors: string[] = []
    page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()) })
    await page.goto(directoryPath)
    const show = page.getByRole('button', { name: 'Show members for Engineering', exact: true })
    await expect(show).toHaveAttribute('aria-expanded', 'false')
    const controls = await show.getAttribute('aria-controls')
    expect(controls).toBeTruthy()
    expect(state.rosterRequests).toEqual([])
    await show.focus()
    // When
    await show.press('Enter')
    // Then
    const hide = page.getByRole('button', { name: 'Hide members for Engineering', exact: true })
    await expect(hide).toHaveAttribute('aria-expanded', 'true')
    await expect(hide).toHaveAttribute('aria-controls', controls ?? '')
    const roster = page.getByRole('region', { name: 'Engineering members', exact: true })
    await expect(roster).toHaveAttribute('id', controls ?? '')
    await expect(roster.getByRole('listitem')).toHaveCount(4)
    await expect(roster.getByText('Alex Morgan', { exact: true })).toHaveCount(2)
    await expect(roster.getByText('Name unavailable', { exact: true })).toHaveCount(2)
    await expect(roster).not.toContainText(/private-roster|@example/)
    expect(state.rosterRequests).toEqual([rosterPath])
    await expect(page.getByRole('button', { name: `Show members for ${siblingDepartment.name}`, exact: true })).toHaveAttribute('aria-expanded', 'false')
    await hide.press('Space')
    await expect(roster).toBeHidden()
    await expect(show).toBeFocused()
    await expect(show).toHaveAttribute('aria-expanded', 'false')
    expect(consoleErrors).toEqual([])
  })

  test('one loading roster leaves its sibling empty state independent', async ({ page }) => {
    // Given
    const state = await prepareLocalDepartments(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route(`**${rosterPath}`, async (route) => {
      state.rosterRequests.push(rosterPath)
      requested.resolve()
      await release.promise
      await route.fulfill({ json: { members: localMembers } })
    })
    await page.goto(directoryPath)
    // When
    await page.getByRole('button', { name: 'Show members for Engineering', exact: true }).click()
    await requested.promise
    try {
      // Then
      const roster = page.getByRole('region', { name: 'Engineering members', exact: true })
      await expect(roster.getByRole('status')).toHaveText('Loading members...')
      await page.getByRole('button', { name: `Show members for ${siblingDepartment.name}`, exact: true }).click()
      const sibling = page.getByRole('region', { name: `${siblingDepartment.name} members`, exact: true })
      await expect(sibling.getByRole('status')).toHaveText('No members in this department yet.')
      await expect(roster.getByRole('status')).toHaveText('Loading members...')
      expect(state.rosterRequests).toEqual([rosterPath, siblingRosterPath])
    } finally {
      release.resolve()
    }
    await expect(page.getByRole('region', { name: 'Engineering members', exact: true }).getByRole('listitem')).toHaveCount(4)
  })

  test('roster errors retry in place without disturbing sibling members or org-admin rename focus', async ({ page }) => {
    // Given
    const state = await prepareLocalDepartments(page, 'org_admin')
    await page.route(`**${siblingRosterPath}`, (route) => route.fulfill({ status: 503, json: { detail: 'Unavailable' } }))
    await page.goto(directoryPath)
    await page.getByRole('button', { name: 'Show members for Engineering', exact: true }).click()
    const roster = page.getByRole('region', { name: 'Engineering members', exact: true })
    await expect(roster.getByRole('listitem')).toHaveCount(4)
    await page.getByRole('button', { name: `Show members for ${siblingDepartment.name}`, exact: true }).click()
    const sibling = page.getByRole('region', { name: `${siblingDepartment.name} members`, exact: true })
    await expect(sibling.getByRole('status')).toContainText('Unable to load department members')
    await page.getByRole('button', { name: 'Edit name for Engineering', exact: true }).click()
    await page.getByLabel('Department name').fill('Product engineering')
    await page.getByLabel('Department name').press('Enter')
    await expect(page.getByRole('button', { name: 'Edit name for Product engineering', exact: true })).toBeFocused()
    const renamedRoster = page.getByRole('region', { name: 'Product engineering members', exact: true })
    await expect(renamedRoster.getByRole('listitem')).toHaveCount(4)
    await expect(page.getByRole('button', { name: 'Hide members for Product engineering', exact: true })).toHaveAttribute('aria-expanded', 'true')
    await page.unroute(`**${siblingRosterPath}`)
    // When
    await sibling.getByRole('button', { name: 'Retry', exact: true }).click()
    // Then
    await expect(sibling.getByRole('status')).toHaveText('No members in this department yet.')
    await expect(renamedRoster.getByRole('listitem')).toHaveCount(4)
    expect(state.rosterRequests).toEqual([rosterPath, siblingRosterPath])
    expect(state.patches).toEqual([{ name: 'Product engineering' }])
    expect(state.optionsRequests).toBe(2)
  })

  for (const status of [200, 401, 503]) {
    test(`same-token workspace replacement fences a held roster ${status} and resets its disclosure`, async ({ page }) => {
      // Given
      await prepareLocalDepartments(page, 'member', originalToken)
      const requested = Promise.withResolvers<void>()
      const release = Promise.withResolvers<void>()
      const handled = Promise.withResolvers<void>()
      const requests: string[] = []
      let replaced = false
      await page.route(`**${rosterPath}`, async (route) => {
        expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
        requests.push(replaced ? 'replacement' : 'original')
        if (replaced) return route.fulfill({ json: { members: [{ ...localMembers[0], display_name: 'Replacement member' }] } })
        requested.resolve()
        await release.promise
        await route.fulfill({ status, json: status === 200
          ? { members: [{ ...localMembers[0], display_name: 'Obsolete member' }] }
          : { detail: 'Retired roster owner' } })
        handled.resolve()
      })
      await page.goto(directoryPath)
      await page.getByRole('button', { name: 'Show members for Engineering', exact: true }).click()
      await requested.promise
      const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => new URL(request.url()).pathname === rosterPath })
      try {
        // When
        await switchFromSettings(page)
        // Then
        await expect(page.getByRole('complementary').getByText(replacementCompany.name, { exact: true })).toBeVisible()
        await cancelled
        replaced = true
        release.resolve()
        await handled.promise
        await page.getByRole('link', { name: 'Directory', exact: true }).click()
        await page.getByRole('button', { name: 'Departments', exact: true }).click()
        const show = page.getByRole('button', { name: 'Show members for Engineering', exact: true })
        await expect(show).toHaveAttribute('aria-expanded', 'false')
        expect(requests).toEqual(['original'])
        await show.click()
        const roster = page.getByRole('region', { name: 'Engineering members', exact: true })
        await expect(roster.getByRole('listitem')).toHaveCount(1)
        await expect(roster.getByText('Replacement member', { exact: true })).toBeVisible()
        await expect(roster.getByRole('status')).toHaveCount(0)
        await expect(page.getByText(/Obsolete member|Retired roster owner/)).toHaveCount(0)
        await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
        expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(originalToken)
        expect(requests).toEqual(['original', 'replacement'])
      } finally {
        release.resolve()
      }
    })
  }

  test('cross-tab replacement cancels a mounted roster and ignores its late unauthorized result', async ({ page }) => {
    // Given
    await prepareLocalDepartments(page)
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const handled = Promise.withResolvers<void>()
    await page.route(`**${rosterPath}`, async (route) => {
      if (route.request().headers().authorization === `Bearer ${replacementToken}`) {
        return route.fulfill({ json: { members: [{ ...localMembers[0], display_name: 'Cross-tab member' }] } })
      }
      requested.resolve()
      await release.promise
      await route.fulfill({ status: 401, json: { detail: 'Obsolete roster owner' } })
      handled.resolve()
    })
    await page.goto(directoryPath)
    await page.getByRole('button', { name: 'Show members for Engineering', exact: true }).click()
    await requested.promise
    const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => new URL(request.url()).pathname === rosterPath })
    try {
      // When
      await writeFromOtherTab(page, replacementToken)
      // Then
      await cancelled
      const show = page.getByRole('button', { name: 'Show members for Engineering', exact: true })
      await expect(show).toHaveAttribute('aria-expanded', 'false')
      release.resolve()
      await handled.promise
      await show.click()
      const roster = page.getByRole('region', { name: 'Engineering members', exact: true })
      await expect(roster.getByRole('listitem')).toHaveCount(1)
      await expect(roster.getByText('Cross-tab member', { exact: true })).toBeVisible()
      await expect(roster.getByRole('status')).toHaveCount(0)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      expect(await page.evaluate((key) => localStorage.getItem(key), tokenKey)).toBe(replacementToken)
    } finally {
      release.resolve()
    }
  })
}
