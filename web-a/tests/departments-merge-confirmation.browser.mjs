import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { mergeConfirmationToken, mergeDestinationId, otherTenant, tenant } from './departments-browser-harness.mjs'
import { seed } from './departments-merge.browser.mjs'

const panel = (page, name = 'Operations') => page.getByRole('group', { name: `Merge “${name}”`, exact: true })
const apply = (page) => panel(page).getByRole('button', { name: 'Merge departments', exact: true })
const checkbox = (page) => panel(page).getByRole('checkbox')
const freshToken = `v1:${'2'.repeat(64)}`

async function review(page, state, base) {
  seed(state)
  await page.goto(`${base}users?company=${tenant}`)
  await page.getByRole('button', { name: 'Merge Operations', exact: true }).click()
  await panel(page).getByLabel('Destination department').selectOption(mergeDestinationId)
  await expect(panel(page).locator('dd')).toHaveText(['2', '2', '1', '3', '4', '2'])
  await checkbox(page).check()
}

export async function runMergeConfirmationCases(harness) {
  await harness.scenario({ name: 'offline uncached preview announces waiting and reconnect never submits a merge', role: 'org_admin' }, async (page, state, base) => {
    // Given
    seed(state)
    await page.goto(`${base}users?company=${tenant}`)
    await page.getByRole('button', { name: 'Merge Operations', exact: true }).click()
    await page.context().setOffline(true)
    // When
    await panel(page).getByLabel('Destination department').selectOption(mergeDestinationId)
    // Then
    await expect(panel(page).getByRole('status').filter({ hasText: /waiting for connection.*merge preview/i })).toBeVisible()
    await expect(apply(page)).toBeDisabled()
    await expect(checkbox(page)).toHaveCount(0)
    assert.equal(state.mergePreviews.length, 0)
    assert.equal(state.merges.length, 0)
    await harness.capture(page, 'merge-preview-offline')
    await page.context().setOffline(false)
    await expect(panel(page).locator('dd')).toHaveText(['2', '2', '1', '3', '4', '2'])
    await expect(checkbox(page)).not.toBeChecked()
    await expect(apply(page)).toBeDisabled()
    await expect(panel(page).getByRole('status').filter({ hasText: /waiting for connection/i })).toHaveCount(0)
    assert.equal(state.mergePreviews.length, 1)
    assert.equal(state.merges.length, 0)
    await harness.capture(page, 'merge-preview-reconnected-unconfirmed')
  })

  for (const change of ['equal-count replacement', 'destination rename']) {
    await harness.scenario({ name: `stale ${change} refreshes preview and requires explicit new consent`, role: 'org_admin' }, async (page, state, base) => {
      // Given
      await review(page, state, base)
      state.mergeToken = freshToken
      state.mergeStatus = 409
      state.mergeDetail = { code: 'department_merge_preview_changed' }
      state.mergePreviewGate = Promise.withResolvers()
      if (change === 'destination rename') state.directories[tenant].departments[1].name = 'Renamed engineering'
      // When
      await apply(page).click()
      // Then
      await expect.poll(() => state.mergePreviews.length).toBe(2)
      await expect(panel(page).getByRole('heading')).toBeFocused()
      await expect(panel(page).getByRole('alert')).toBeVisible()
      await expect(apply(page)).toBeDisabled()
      await expect(checkbox(page)).toHaveCount(0)
      assert.equal(state.merges.length, 1)
      assert.deepEqual(state.merges[0].payload, { destination_id: mergeDestinationId, confirmation_token: mergeConfirmationToken })
      await harness.capture(page, `merge-stale-${change.replaceAll(' ', '-')}-loading`)
      state.mergePreviewGate.resolve()
      await expect(panel(page).locator('dd')).toHaveText(['2', '2', '1', '3', '4', '2'])
      if (change === 'destination rename') await expect(panel(page).getByText('“Operations” into “Renamed engineering”', { exact: true })).toBeVisible()
      await expect(checkbox(page)).not.toBeChecked()
      await expect(apply(page)).toBeDisabled()
      await page.keyboard.press('Enter')
      assert.equal(state.merges.length, 1)
      await harness.capture(page, `merge-stale-${change.replaceAll(' ', '-')}-review`)
      state.mergeStatus = 200
      await page.keyboard.press('Tab')
      await page.keyboard.press('Tab')
      await expect(checkbox(page)).toBeFocused()
      await page.keyboard.press('Space')
      await page.keyboard.press('Tab')
      await page.keyboard.press('Tab')
      await expect(apply(page)).toBeFocused()
      await page.keyboard.press('Enter')
      await expect(panel(page)).toHaveCount(0)
      assert.equal(state.merges.length, 2)
      assert.deepEqual(state.merges[1].payload, { destination_id: mergeDestinationId, confirmation_token: freshToken })
    })
  }

  await harness.scenario({ name: 'stale preview refresh failure keeps consent cleared until explicit retry and review' }, async (page, state, base) => {
    // Given
    await review(page, state, base)
    state.mergeStatus = 409
    state.mergeDetail = { code: 'department_merge_preview_changed' }
    state.mergePreviewStatus = 503
    // When
    await apply(page).click()
    // Then
    await expect(panel(page).getByRole('button', { name: 'Retry preview' })).toBeVisible()
    await expect(apply(page)).toBeDisabled()
    assert.equal(state.mergePreviews.length, 2)
    assert.equal(state.merges.length, 1)
    state.mergePreviewStatus = 200
    state.mergeToken = freshToken
    await panel(page).getByRole('button', { name: 'Retry preview' }).click()
    await expect(checkbox(page)).not.toBeChecked()
    await expect(apply(page)).toBeDisabled()
    assert.equal(state.merges.length, 1)
  })

  for (const transition of ['destination', 'tenant', 'session']) {
    await harness.scenario({ name: `late stale-refresh preview cannot restore consent after ${transition} changes` }, async (page, state, base) => {
      // Given
      await review(page, state, base)
      state.mergeStatus = 409
      state.mergeDetail = { code: 'department_merge_preview_changed' }
      state.mergePreviewGate = Promise.withResolvers()
      await apply(page).click()
      await expect.poll(() => state.mergePreviews.length).toBe(2)
      const stale = state.mergePreviews[1]
      state.mergePreviewGate = null
      state.mergeToken = freshToken
      // When
      if (transition === 'destination') {
        await panel(page).getByLabel('Destination department').selectOption('')
        await panel(page).getByLabel('Destination department').selectOption(mergeDestinationId)
      } else if (transition === 'tenant') {
        await page.getByRole('combobox').first().selectOption(otherTenant)
        await page.getByRole('button', { name: 'Merge Beta operations', exact: true }).click()
        await panel(page, 'Beta operations').getByLabel('Destination department').selectOption(mergeDestinationId)
      } else {
        await page.getByRole('button', { name: 'Sign out', exact: true }).click()
        await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
        await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
        await page.getByRole('button', { name: 'Sign in', exact: true }).click()
        await page.getByRole('link', { name: 'Users', exact: true }).click()
        await page.getByRole('combobox').first().selectOption(tenant)
        await page.getByRole('button', { name: 'Merge Operations', exact: true }).click()
        await panel(page).getByLabel('Destination department').selectOption(mergeDestinationId)
      }
      // Then
      const current = panel(page, transition === 'tenant' ? 'Beta operations' : 'Operations')
      await expect(current.getByRole('checkbox')).not.toBeChecked()
      stale.gate.resolve()
      await stale.delivered.promise
      await expect(current.getByRole('checkbox')).not.toBeChecked()
      await expect(current.getByRole('button', { name: 'Merge departments', exact: true })).toBeDisabled()
      await expect(current.getByRole('alert')).toHaveCount(0)
      assert.equal(state.merges.length, 1)
      await page.keyboard.press('Escape')
      await expect(current).toHaveCount(0)
      await expect(page.getByRole('button', { name: transition === 'tenant' ? 'Merge Beta operations' : 'Merge Operations', exact: true })).toBeFocused()
    })
  }
}
