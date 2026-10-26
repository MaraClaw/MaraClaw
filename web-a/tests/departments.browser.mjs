import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { confirmation, createHarness, departmentId, directory, openUsers, otherTenant, tenant } from './departments-browser-harness.mjs'
import { runMergeCases } from './departments-merge.browser.mjs'

const harness = await createHarness()
let completed = false
const deletes = (state) => state.calls.filter((call) => call.method === 'DELETE')
try {
  await harness.scenario({ name: 'keyboard confirmation never submits on opening; Escape and Cancel restore focus' }, async (page, state, base) => {
    // Given
    await openUsers(page, base)
    const trigger = page.getByRole('button', { name: 'Delete Operations', exact: true })
    await trigger.focus()
    // When
    await page.keyboard.press('Enter')
    // Then
    await expect(confirmation(page).getByRole('heading')).toBeFocused()
    await page.keyboard.press('Enter')
    assert.equal(deletes(state).length, 0)
    await page.keyboard.press('Escape')
    await expect(trigger).toBeFocused()
    await page.keyboard.press('Space')
    await expect(confirmation(page).getByRole('heading')).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(confirmation(page).getByRole('button', { name: 'Cancel', exact: true })).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(trigger).toBeFocused()
    assert.equal(deletes(state).length, 0)
  })

  await harness.scenario({ name: 'keyboard deletion is pending once, then removes assignments and refreshes tenant users' }, async (page, state, base) => {
    // Given
    state.gate = Promise.withResolvers()
    await openUsers(page, base)
    await page.getByRole('button', { name: 'Delete Operations', exact: true }).click()
    await expect(confirmation(page).getByRole('button', { name: 'Delete department', exact: true })).toBeEnabled()
    await page.keyboard.press('Tab')
    await page.keyboard.press('Tab')
    // When
    await page.keyboard.press('Enter')
    // Then
    await expect(confirmation(page)).toHaveAttribute('aria-busy', 'true')
    await expect(confirmation(page).getByRole('button', { name: 'Delete department', exact: true })).toBeDisabled()
    await expect(confirmation(page).getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled()
    await expect(confirmation(page).getByRole('status').filter({ hasText: 'Deleting department' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(confirmation(page)).toBeVisible()
    assert.equal(deletes(state).length, 1)
    assert.deepEqual(deletes(state)[0], { method: 'DELETE', path: `/api/departments/${encodeURIComponent(departmentId)}`, params: { tenant_id: tenant } })
    await harness.capture(page, 'deletion-pending-keyboard')
    state.gate.resolve()
    await expect(confirmation(page)).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Delete Operations', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Delete Engineering', exact: true })).toBeVisible()
    await expect(page.getByLabel('Department name', { exact: true })).toBeFocused()
    const memberships = page.getByRole('group', { name: 'Departments for Member', exact: true })
    await expect(memberships.getByLabel('Operations', { exact: true })).toHaveCount(0)
    await expect(memberships.getByLabel('Engineering', { exact: true })).toBeChecked()
    await expect.poll(() => state.calls.filter((call) => call.path === '/api/users/page').length).toBe(2)
  })

  await harness.scenario({ name: 'preview failure blocks deletion; retry accepts zero associations' }, async (page, state, base) => {
    // Given
    state.previewStatus = 503
    await openUsers(page, base)
    // When
    await page.getByRole('button', { name: 'Delete Operations', exact: true }).click()
    // Then
    await expect(confirmation(page).getByRole('alert')).toBeVisible()
    await expect(confirmation(page).getByRole('button', { name: 'Delete department', exact: true })).toBeDisabled()
    assert.equal(deletes(state).length, 0)
    await harness.capture(page, 'preview-error')
    state.previewStatus = 200
    state.preview = { membership_count: 0, agent_grant_count: 0 }
    await confirmation(page).getByRole('button', { name: 'Retry preview' }).click()
    await expect(confirmation(page).locator('dd')).toHaveText(['0', '0'])
    await expect(confirmation(page).getByRole('button', { name: 'Delete department', exact: true })).toBeEnabled()
    assert.equal(state.previews.length, 2)
    await expect(confirmation(page).getByRole('heading')).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(confirmation(page)).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Delete Operations', exact: true })).toBeFocused()
  })

  for (const transition of ['reopen', 'tenant', 'session']) for (const status of [200, 503]) {
    await harness.scenario({ name: `late preview ${status} cannot affect ${transition} confirmation` }, async (page, state, base) => {
      // Given
      state.previewStatus = status
      state.preview = { membership_count: 91, agent_grant_count: 82 }
      state.previewGate = Promise.withResolvers()
      await openUsers(page, base)
      await page.getByRole('button', { name: 'Delete Operations', exact: true }).click()
      await expect.poll(() => state.previews.length).toBe(1)
      await expect(confirmation(page).getByRole('button', { name: 'Delete department', exact: true })).toBeDisabled()
      await harness.capture(page, `preview-loading-${transition}-${status}`)
      // When
      const old = state.previews[0]
      if (transition === 'reopen') await page.keyboard.press('Escape')
      else if (transition === 'tenant') await page.getByRole('combobox').selectOption(otherTenant)
      else {
        await page.getByRole('button', { name: 'Sign out', exact: true }).click()
        await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
        await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
        await page.getByRole('button', { name: 'Sign in', exact: true }).click()
        await page.getByRole('link', { name: 'Users', exact: true }).click()
        await page.getByRole('combobox').selectOption(tenant)
      }
      state.previewGate = null
      state.previewStatus = 200
      state.preview = { membership_count: 2, agent_grant_count: 4 }
      const name = transition === 'tenant' ? 'Beta operations' : 'Operations'
      await page.getByRole('button', { name: `Delete ${name}`, exact: true }).click()
      await expect(confirmation(page, name).locator('dd')).toHaveText(['2', '4'])
      old.gate.resolve()
      await old.delivered.promise
      // Then
      await expect(confirmation(page, name).locator('dd')).toHaveText(['2', '4'])
      await expect(confirmation(page, name).getByRole('alert')).toHaveCount(0)
      assert.equal(deletes(state).length, 0)
    })
  }

  await harness.scenario({ name: 'delete error keeps the card and assignments; keyboard retry succeeds' }, async (page, state, base) => {
    // Given
    state.deleteStatus = 403
    await openUsers(page, base)
    await page.getByRole('button', { name: 'Delete Operations', exact: true }).click()
    // When
    await confirmation(page).getByRole('button', { name: 'Delete department', exact: true }).click()
    // Then
    await expect(confirmation(page).getByRole('alert')).toHaveText('Synthetic department deletion denied')
    await expect(confirmation(page).getByRole('button', { name: 'Delete department', exact: true })).toBeEnabled()
    await expect(page.getByRole('group', { name: 'Departments for Member', exact: true }).getByLabel('Operations', { exact: true })).toBeChecked()
    await harness.capture(page, 'deletion-error')
    state.deleteStatus = 204
    await confirmation(page).getByRole('button', { name: 'Delete department', exact: true }).focus()
    await page.keyboard.press('Enter')
    await expect(confirmation(page)).toHaveCount(0)
    assert.equal(deletes(state).length, 2)
  })

  for (const status of [204, 403]) for (const transition of ['tenant', 'session']) {
    await harness.scenario({ name: `late ${status} deletion cannot affect a replacement ${transition}` }, async (page, state, base) => {
      // Given
      state.deleteStatus = status
      state.gate = Promise.withResolvers()
      await openUsers(page, base)
      await page.getByRole('button', { name: 'Delete Operations', exact: true }).click()
      await confirmation(page).getByRole('button', { name: 'Delete department', exact: true }).click()
      await expect.poll(() => state.exchanges.length).toBe(1)
      // When
      if (transition === 'tenant') {
        await page.getByRole('combobox').selectOption(otherTenant)
      } else {
        await page.getByRole('button', { name: 'Sign out', exact: true }).click()
        state.role = 'org_admin'
        state.directories[tenant] = directory('Replacement operations')
        await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
        await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
        await page.getByRole('button', { name: 'Sign in', exact: true }).click()
        await page.getByRole('link', { name: 'Users', exact: true }).click()
      }
      // Then
      const name = transition === 'tenant' ? 'Beta operations' : 'Replacement operations'
      await expect(page.getByRole('button', { name: `Delete ${name}`, exact: true })).toBeVisible()
      const before = state.calls.filter((call) => call.path === '/api/departments').length
      state.gate.resolve()
      await state.exchanges[0].delivered.promise
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
      await expect(page.getByRole('button', { name: `Delete ${name}`, exact: true })).toBeVisible()
      await expect(page.getByText('Synthetic department deletion denied', { exact: true })).toHaveCount(0)
      await expect(page.getByText('Deleted department “Operations”', { exact: true })).toHaveCount(0)
      assert.equal(state.calls.filter((call) => call.path === '/api/departments').length, before)
    })
  }

  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) {
    await harness.scenario({ name: `confirmation ${width} ${scheme} reflows without overflow`, mobile: width === 390, width, scheme }, async (page, _state, base) => {
      // Given
      await openUsers(page, base)
      await harness.capture(page, `departments-${width}-${scheme}-rest`)
      // When
      await page.getByRole('button', { name: 'Delete Operations', exact: true }).click()
      // Then
      await expect(confirmation(page)).toBeVisible()
      await expect(confirmation(page).locator('dd')).toHaveText(['1', '3'])
      for (const position of ['top', 'end']) {
        await page.locator('main').evaluate((main, end) => { main.scrollTop = end ? main.scrollHeight : 0 }, position === 'end')
        await harness.capture(page, `departments-${width}-${scheme}-confirm-${position}`)
      }
    })
  }
  await runMergeCases(harness)
  completed = true
} finally { await harness.close(completed) }
