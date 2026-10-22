import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { admission, createHarness } from './departments-browser-harness.mjs'

const harness = await createHarness()
let completed = false
try {
  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) {
    await harness.scenario({ name: `withdrawn listing and read-only detail ${width} ${scheme}`, role: 'org_admin', mobile: width === 390, width, scheme }, async (page, state, base) => {
      // Given
      await page.goto(`${base}admission-approvals`)
      await expect(page.getByRole('heading', { name: 'Admission approvals', exact: true })).toBeVisible()
      // When
      await page.getByLabel('Request status', { exact: true }).selectOption('withdrawn')
      // Then
      await expect(page.getByRole('list', { name: 'Admission requests', exact: true })).toContainText('Withdrawn')
      assert.equal(state.calls.filter((call) => call.path === '/api/company-admissions/review').at(-1).params.status, 'withdrawn')
      await harness.capture(page, `withdrawn-${width}-${scheme}-list`)
      await page.getByRole('link', { name: `Review request from ${admission.applicant.email}`, exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Request details', exact: true })).toBeVisible()
      await expect(page.getByText('Withdrawn', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: /^(Approve|Reject) request$/ })).toHaveCount(0)
      await expect(page.getByLabel('Reason (optional)', { exact: true })).toHaveCount(0)
      await expect(page.getByRole('button', { name: /withdraw/i })).toHaveCount(0)
      for (const position of ['top', 'end']) {
        await page.locator('main').evaluate((main, end) => { main.scrollTop = end ? main.scrollHeight : 0 }, position === 'end')
        await harness.capture(page, `withdrawn-${width}-${scheme}-detail-${position}`)
      }
      assert.equal(state.calls.some((call) => call.method !== 'GET'), false)
    })
  }
  completed = true
} finally { await harness.close(completed) }
