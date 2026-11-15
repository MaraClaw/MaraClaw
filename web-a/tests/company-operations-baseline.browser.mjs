// COMPANY_OPERATIONS_ARTIFACTS=/owned/evidence node tests/company-operations-baseline.browser.mjs
import { expect } from '@playwright/test'
import { company, companyHarness } from './company-operations-harness.mjs'

const harness = await companyHarness()
try {
  for (const mobile of [false, true]) for (const theme of ['light', 'dark']) {
    const name = `detail-${mobile ? '390' : '1440'}-${theme}-before`
    await harness.scenario(name, { mobile, theme }, async (page, _state, base) => {
      await page.goto(`${base}companies/${company.id}`)
      await expect(page.getByRole('button', { name: 'Preview deletion' })).toBeVisible()
      await harness.capture(page, name)
    })
  }
} finally { await harness.close() }
