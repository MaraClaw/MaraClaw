import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { createHarness, openUsers, tenant } from './departments-browser-harness.mjs'

const harness = await createHarness()
const baseline = process.env.DEPARTMENT_BASELINE === '1'
let completed = false
try {
  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) {
    await harness.scenario({ name: `description forms ${width} ${scheme}`, mobile: width === 390, width, scheme }, async (page, state, base) => {
      // Given
      await openUsers(page, base)
      const suffix = `${width}-${scheme}-${baseline ? 'before' : 'after'}`
      await harness.capture(page, `description-list-${suffix}`)
      await page.getByRole('button', { name: baseline ? 'Edit name for Operations' : 'Edit department for Operations', exact: true }).click()
      await harness.capture(page, `description-edit-${suffix}`)
      if (baseline) return
      const editor = page.getByRole('form', { name: 'Edit Operations', exact: true })
      await expect(editor.getByLabel('Description (optional)', { exact: true })).toHaveValue(state.directories[tenant].departments[0].description)
      await editor.getByRole('button', { name: 'Cancel', exact: true }).click()
      const description = '  Owns escalations\n\nBilling and onboarding  '
      const name = 'Customer support'
      // When: create through the actual form
      await page.getByLabel('Department name', { exact: true }).fill(name)
      await page.getByLabel('Description (optional)', { exact: true }).fill(description)
      await page.getByRole('button', { name: 'Create department', exact: true }).click()
      // Then: exact request, list value and reset
      const card = page.getByRole('list', { name: 'Departments', exact: true }).getByRole('listitem').filter({ has: page.getByRole('button', { name: `Edit department for ${name}`, exact: true }) })
      await expect(card.locator('p').filter({ hasText: 'Owns escalations' })).toHaveText(description, { useInnerText: false })
      assert.equal(await card.locator('p').nth(1).textContent(), description)
      assert.deepEqual(state.calls.find((call) => call.method === 'POST' && call.path === '/api/departments').payload, { name, description })
      await expect(page.getByLabel('Description (optional)', { exact: true })).toHaveValue('')
      await harness.capture(page, `description-created-${suffix}`)
      // When: same-name description-only edit
      await card.getByRole('button', { name: `Edit department for ${name}`, exact: true }).click()
      const edit = page.getByRole('form', { name: `Edit ${name}`, exact: true })
      const updated = '\n  Billing ownership\nEscalation review  \n'
      await expect(edit.getByLabel('Description (optional)', { exact: true })).toHaveValue(description)
      await edit.getByLabel('Description (optional)', { exact: true }).fill(updated)
      await edit.getByRole('button', { name: 'Save department', exact: true }).click()
      // Then
      await expect(card.locator('p').filter({ hasText: 'Billing ownership' })).toHaveText(updated, { useInnerText: false })
      assert.equal(await card.locator('p').nth(1).textContent(), updated)
      assert.deepEqual(state.calls.filter((call) => call.method === 'PATCH').at(-1).payload, { name, description: updated })
      // When: explicit clear
      await card.getByRole('button', { name: `Edit department for ${name}`, exact: true }).click()
      await edit.getByLabel('Description (optional)', { exact: true }).fill('')
      await edit.getByRole('button', { name: 'Save department', exact: true }).click()
      // Then
      await expect(card.getByText('No description', { exact: true })).toBeVisible()
      assert.deepEqual(state.calls.filter((call) => call.method === 'PATCH').at(-1).payload, { name, description: '' })
      await page.locator('main').evaluate((main) => { main.scrollTop = main.scrollHeight })
      await harness.capture(page, `description-cleared-end-${suffix}`)
    })
  }
  completed = true
} finally { await harness.close(completed) }
