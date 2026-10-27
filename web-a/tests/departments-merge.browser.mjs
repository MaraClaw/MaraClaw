import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { mergeConfirmationToken, mergeDestinationId, mergeSourceId, otherTenant, tenant } from './departments-browser-harness.mjs'

const panel = (page, name = 'Operations') => page.getByRole('group', { name: `Merge “${name}”`, exact: true })
const apply = (page) => panel(page).getByRole('button', { name: 'Merge departments', exact: true })
const checkbox = (page) => panel(page).getByRole('checkbox')
export function seed(state) {
  for (const [tenantId, directory] of Object.entries(state.directories)) {
    directory.departments[0].id = mergeSourceId
    directory.departments[1].id = mergeDestinationId
    directory.assignments = [
      { user_id: 'Member', tenant_id: tenantId, department_id: mergeSourceId },
      { user_id: 'Member', tenant_id: tenantId, department_id: mergeDestinationId },
      { user_id: 'Source-only', tenant_id: tenantId, department_id: mergeSourceId },
    ]
  }
}
async function open(page, state, base) {
  seed(state)
  await page.goto(`${base}users?company=${tenant}`)
  await page.getByRole('button', { name: 'Merge Operations', exact: true }).click()
}
async function select(page) {
  await panel(page).getByLabel('Destination department').selectOption(mergeDestinationId)
  await expect(panel(page).locator('dd')).toHaveText(['2', '2', '1', '3', '4', '2'])
}

export async function runMergeCases(harness) {
  await harness.scenario({ name: 'merge keyboard selection requires explicit confirmation; Escape and Cancel restore focus', role: 'org_admin' }, async (page, state, base) => {
    await open(page, state, base)
    await expect(panel(page).getByRole('heading')).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(apply(page)).toBeDisabled()
    await page.keyboard.press('Tab')
    await expect(panel(page).getByLabel('Destination department')).toBeFocused()
    await page.keyboard.press('e')
    await expect(panel(page).locator('dd')).toHaveCount(6)
    await expect(checkbox(page)).not.toBeChecked()
    await expect(apply(page)).toBeDisabled()
    await expect(panel(page).getByLabel('Destination department').locator('option')).toHaveText(['Select a department', 'Engineering'])
    assert.equal(state.merges.length, 0)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('button', { name: 'Merge Operations', exact: true })).toBeFocused()
    await page.keyboard.press('Space')
    await panel(page).getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Merge Operations', exact: true })).toBeFocused()
  })

  await harness.scenario({ name: 'atomic merge preserves overlaps and access explanation; pending locks and refreshes users', role: 'org_admin' }, async (page, state, base) => {
    state.mergeGate = Promise.withResolvers()
    await open(page, state, base)
    await select(page)
    await expect(panel(page).getByText(/Members joining the destination also gain/)).toBeVisible()
    const usersBefore = state.calls.filter((call) => call.path === '/api/users/page').length
    await checkbox(page).check()
    await apply(page).focus()
    await page.keyboard.press('Enter')
    await expect(panel(page)).toHaveAttribute('aria-busy', 'true')
    await expect(panel(page).getByLabel('Destination department')).toBeDisabled()
    await expect(checkbox(page)).toBeDisabled()
    await expect(panel(page).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await page.keyboard.press('Enter')
    await page.keyboard.press('Escape')
    assert.equal(state.merges.length, 1)
    assert.deepEqual(state.merges[0].payload, { destination_id: mergeDestinationId, confirmation_token: mergeConfirmationToken })
    assert.deepEqual(state.merges[0].params, { tenant_id: tenant })
    assert.equal(state.calls.filter((call) => call.method === 'DELETE' || call.method === 'PUT').length, 0)
    await harness.capture(page, 'merge-pending')
    state.mergeGate.resolve()
    await expect(panel(page)).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Merge Operations', exact: true })).toHaveCount(0)
    await expect(page.getByLabel('Department name', { exact: true })).toBeFocused()
    await expect(page.getByRole('group', { name: 'Departments for Member', exact: true }).getByLabel('Engineering', { exact: true })).toBeChecked()
    assert.equal(state.directories[tenant].assignments.filter((item) => item.user_id === 'Member').length, 1)
    await expect.poll(() => state.calls.filter((call) => call.path === '/api/users/page').length).toBe(usersBefore + 1)
    await harness.capture(page, 'merge-success')
  })

  await harness.scenario({ name: 'merge preview and apply failures recover without success; selection resets consent' }, async (page, state, base) => {
    state.mergePreviewStatus = 503
    await open(page, state, base)
    await panel(page).getByLabel('Destination department').selectOption(mergeDestinationId)
    await expect(panel(page).getByRole('alert')).toBeVisible()
    await expect(apply(page)).toBeDisabled()
    await harness.capture(page, 'merge-preview-error')
    state.mergePreviewStatus = 200
    await panel(page).getByRole('button', { name: 'Retry preview' }).click()
    await expect(panel(page).getByRole('heading')).toBeFocused()
    await expect(panel(page).locator('dd')).toHaveCount(6)
    await checkbox(page).check()
    await panel(page).getByLabel('Destination department').selectOption('')
    await expect(apply(page)).toBeDisabled()
    await select(page)
    await expect(checkbox(page)).not.toBeChecked()
    state.mergeStatus = 409
    await checkbox(page).check()
    await apply(page).click()
    await expect(panel(page).getByRole('alert')).toHaveText('Synthetic merge denied')
    await expect(panel(page).getByRole('heading')).toBeFocused()
    await expect(page.getByText('Merged “Operations” into “Engineering”', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('group', { name: 'Departments for Member', exact: true }).getByLabel('Operations', { exact: true })).toBeChecked()
    await harness.capture(page, 'merge-apply-error')
    state.mergeStatus = 200
    await apply(page).focus()
    await page.keyboard.press('Enter')
    await expect(panel(page)).toHaveCount(0)
    assert.equal(state.merges.length, 2)
  })

  for (const status of [200, 403, 409]) for (const transition of ['tenant', 'session']) {
    await harness.scenario({ name: `late merge ${status} leaves replacement ${transition} untouched` }, async (page, state, base) => {
      state.mergeStatus = status
      if (status === 409) state.mergeDetail = { code: 'department_merge_preview_changed' }
      state.mergeGate = Promise.withResolvers()
      await open(page, state, base)
      await select(page)
      await checkbox(page).check()
      await apply(page).click()
      await expect.poll(() => state.merges.length).toBe(1)
      if (transition === 'tenant') await page.getByRole('combobox').first().selectOption(otherTenant)
      else {
        await page.getByRole('button', { name: 'Sign out', exact: true }).click()
        await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
        await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
        await page.getByRole('button', { name: 'Sign in', exact: true }).click()
        await page.getByRole('link', { name: 'Users', exact: true }).click()
        await page.getByRole('combobox').first().selectOption(tenant)
      }
      const name = transition === 'tenant' ? 'Beta operations' : 'Operations'
      await expect(page.getByRole('button', { name: `Merge ${name}`, exact: true })).toBeVisible()
      const before = state.calls.filter((call) => call.path === '/api/departments' && call.params.tenant_id === (transition === 'tenant' ? otherTenant : tenant)).length
      const previewsBefore = state.mergePreviews.length
      state.mergeGate.resolve()
      await state.merges[0].delivered.promise
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
      await expect(page.getByRole('button', { name: `Merge ${name}`, exact: true })).toBeVisible()
      await expect(page.getByText('Synthetic merge denied', { exact: true })).toHaveCount(0)
      await expect(page.getByText('Merged “Operations” into “Engineering”', { exact: true })).toHaveCount(0)
      assert.equal(state.calls.filter((call) => call.path === '/api/departments' && call.params.tenant_id === (transition === 'tenant' ? otherTenant : tenant)).length, before)
      assert.equal(state.mergePreviews.length, previewsBefore)
    })
  }

  await harness.scenario({ name: 'late merge preview cannot populate a reopened selection' }, async (page, state, base) => {
    state.mergePreviewGate = Promise.withResolvers()
    state.mergeCounts = { source_membership_count: 91 }
    await open(page, state, base)
    await panel(page).getByLabel('Destination department').selectOption(mergeDestinationId)
    await expect.poll(() => state.mergePreviews.length).toBe(1)
    await expect(apply(page)).toBeDisabled()
    await page.keyboard.press('Escape')
    state.mergePreviewGate = null
    state.mergeCounts = { source_membership_count: 2 }
    await page.getByRole('button', { name: 'Merge Operations', exact: true }).click()
    await select(page)
    state.mergePreviews[0].gate.resolve()
    await state.mergePreviews[0].delivered.promise
    await expect(panel(page).locator('dd')).toHaveText(['2', '2', '1', '3', '4', '2'])
    await expect(checkbox(page)).not.toBeChecked()
    await expect(apply(page)).toBeDisabled()
  })

  await harness.scenario({ name: 'merge offers no source or foreign-tenant destination and explains an empty selection' }, async (page, state, base) => {
    seed(state)
    state.directories[tenant].departments[1].tenant_id = otherTenant
    await page.goto(`${base}users?company=${tenant}`)
    await page.getByRole('button', { name: 'Merge Operations', exact: true }).click()
    await expect(panel(page).getByLabel('Destination department')).toBeDisabled()
    await expect(panel(page).getByText('Create another department before merging.', { exact: true })).toBeVisible()
    await expect(apply(page)).toBeDisabled()
    assert.equal(state.mergePreviews.length, 0)
    assert.equal(state.merges.length, 0)
    await harness.capture(page, 'merge-no-destination')
  })

  await harness.scenario({ name: 'zero-association merge still requires consent' }, async (page, state, base) => {
    state.mergeCounts = Object.fromEntries(['source_membership_count', 'destination_membership_count', 'overlapping_membership_count', 'source_agent_grant_count', 'destination_agent_grant_count', 'overlapping_agent_grant_count'].map((key) => [key, 0]))
    await open(page, state, base)
    await panel(page).getByLabel('Destination department').selectOption(mergeDestinationId)
    await expect(panel(page).locator('dd')).toHaveText(['0', '0', '0', '0', '0', '0'])
    await expect(apply(page)).toBeDisabled()
    await checkbox(page).check()
    await expect(apply(page)).toBeEnabled()
    assert.equal(state.merges.length, 0)
  })

  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) {
    await harness.scenario({ name: `merge ${width} ${scheme} advisory preview reflows`, width, mobile: width === 390, scheme }, async (page, state, base) => {
      await open(page, state, base)
      await harness.capture(page, `merge-${width}-${scheme}-selection`)
      await select(page)
      await checkbox(page).check()
      for (const position of ['top', 'controls', 'end']) {
        await panel(page).getByRole('heading').scrollIntoViewIfNeeded()
        if (position === 'controls') await apply(page).scrollIntoViewIfNeeded()
        if (position === 'end') await page.locator('main').evaluate((main) => { main.scrollTop = main.scrollHeight })
        await harness.capture(page, `merge-${width}-${scheme}-preview-${position}`)
      }
    })
  }
}
