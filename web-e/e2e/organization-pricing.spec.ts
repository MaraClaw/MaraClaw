import { expect, test } from '@playwright/test'
import {
  OrganizationAdditionalAgentMonthlyUsd, OrganizationAgentLimit, OrganizationAnnualPriceUsd, OrganizationFileLimitBytes,
  OrganizationIncludedAgents, OrganizationStorageLimitBytes,
} from '../src/lib/engine-contracts.generated.ts'

const plans = [
  { id: 'pro', name: 'Pro', currency: 'USD', monthly_price_usd: 79, annual_price_usd: 790, included_accounts: 5, additional_account_monthly_usd: 5, max_agents_per_account: 2, included_agents: 2, additional_agent_monthly_usd: 35, storage_limit_bytes: 2147483648, max_file_size_bytes: 26214400, daily_messages_per_agent: 100, guest_memory_bytes: 2147483648, guest_millicpus: 1500, guest_cpu_shares: 1024, guest_pids_limit: 512, workspace_disk_bytes: 3221225472, storage_addon_bytes: 10737418240, storage_addon_monthly_usd: 5, workspace_disk_addon_bytes: 5368709120, workspace_disk_addon_monthly_usd: 3 },
  { id: 'max', name: 'Max', currency: 'USD', monthly_price_usd: 249, annual_price_usd: 2490, included_accounts: 15, additional_account_monthly_usd: 4, max_agents_per_account: 3, included_agents: 5, additional_agent_monthly_usd: 49, storage_limit_bytes: 5368709120, max_file_size_bytes: 52428800, daily_messages_per_agent: 200, guest_memory_bytes: 3221225472, guest_millicpus: 2000, guest_cpu_shares: 1536, guest_pids_limit: 768, workspace_disk_bytes: 5368709120, storage_addon_bytes: 10737418240, storage_addon_monthly_usd: 5, workspace_disk_addon_bytes: 5368709120, workspace_disk_addon_monthly_usd: 3 },
  { id: 'ultra', name: 'Ultra', currency: 'USD', monthly_price_usd: 699, annual_price_usd: 6990, included_accounts: 40, additional_account_monthly_usd: 3, max_agents_per_account: 5, included_agents: 10, additional_agent_monthly_usd: 79, storage_limit_bytes: 10737418240, max_file_size_bytes: 104857600, daily_messages_per_agent: 500, guest_memory_bytes: 5368709120, guest_millicpus: 3000, guest_cpu_shares: 2048, guest_pids_limit: 1024, workspace_disk_bytes: 8589934592, storage_addon_bytes: 10737418240, storage_addon_monthly_usd: 5, workspace_disk_addon_bytes: 5368709120, workspace_disk_addon_monthly_usd: 3 },
]

test('catalog loading and error recover through retry and render live allocations', async ({ page }) => {
  let fail = true
  const held = Promise.withResolvers<void>()
  await page.route('**/api/**', async (route) => {
    expect(new URL(route.request().url()).pathname).toBe('/api/organization-plans')
    expect(route.request().method()).toBe('GET')
    await held.promise
    await route.fulfill(fail ? { status: 503, json: {} } : { json: { plans: plans.map((plan) => ({ ...plan, monthly_price_usd: plan.monthly_price_usd + 1, annual_price_usd: OrganizationAnnualPriceUsd.ULTRA, included_agents: OrganizationIncludedAgents.ULTRA, additional_agent_monthly_usd: OrganizationAdditionalAgentMonthlyUsd.ULTRA, max_agents_per_account: OrganizationAgentLimit.ULTRA, storage_limit_bytes: OrganizationStorageLimitBytes.ULTRA, max_file_size_bytes: OrganizationFileLimitBytes.ULTRA, storage_addon_monthly_usd: 6, workspace_disk_addon_bytes: 10737418240 })) } })
  })
  try {
    await page.goto('/')
    await expect(page.getByRole('status').filter({ hasText: 'Loading organization plans' })).toBeVisible()
    held.resolve()
    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toContainText('could not be loaded')
    fail = false
    await page.getByRole('button', { name: 'Retry plans' }).click()
    await expect(page.getByRole('radio', { name: 'Pro', exact: true })).toBeChecked()
    const monthly = page.getByRole('radio', { name: 'Pay monthly', exact: true })
    const annually = page.getByRole('radio', { name: 'Pay annually Save up to 17%', exact: true })
    await expect(monthly).toBeChecked()
    const card = (name: string) => page.locator('label').filter({ has: page.getByRole('radio', { name, exact: true }) })
    for (const plan of plans) {
      await expect(card(plan.name)).toContainText(`$${plan.monthly_price_usd + 1}USD / month`)
      await expect(card(plan.name)).toContainText('Billed monthly')
      await expect(card(plan.name)).not.toContainText('year')
      await expect(card(plan.name)).toContainText(`${OrganizationIncludedAgents.ULTRA} agents included`)
      await expect(card(plan.name)).toContainText(`$${OrganizationAdditionalAgentMonthlyUsd.ULTRA} USD / additional agent / month`)
      for (const removed of ['agents per account', 'messages per agent', 'per managed S3 file', 'Add-on']) await expect(card(plan.name)).not.toContainText(removed)
    }
    await page.locator('label').filter({ has: annually }).click()
    await expect(annually).toBeChecked()
    await expect(page.getByRole('radio', { name: 'Pro', exact: true })).toBeChecked()
    for (const plan of plans) await expect(card(plan.name)).toContainText('$6,990USD / year')
    await expect(card('Ultra')).toContainText('Billed annually, save $1,410 USD')
    await expect(card('Pro')).toContainText('Billed annually')
    await expect(card('Pro')).not.toContainText('save')
    await page.locator('label').filter({ has: monthly }).click()
    await expect(card('Ultra')).toContainText(`$${plans[2].monthly_price_usd + 1}USD / month`)
    const resources = page.getByRole('table', { name: 'Resources by plan' })
    await expect(resources.getByRole('row', { name: /^Pro / })).toHaveText(/Pro1\.5 vCPU2 GiB100 GiB total10 GiB per agent/)
    await expect(resources.getByRole('row', { name: /^Max / })).toHaveText(/Max2 vCPU3 GiB100 GiB total10 GiB per agent/)
    await expect(resources.getByRole('row', { name: /^Ultra / })).toHaveText(/Ultra3 vCPU5 GiB100 GiB total10 GiB per agent/)
    await expect(page.locator('#pricing-terms')).toContainText('billed monthly by bank transfer')
    await expect(page.locator('#pricing-models')).toContainText('Grok, ChatGPT or Claude subscription')
    await expect(page.locator('#pricing-models')).toContainText('BYOK')
  } finally { held.resolve() }
})

test('email validation, keyboard tier selection, disabled in-flight controls and retry preserve inputs', async ({ page }, testInfo) => {
  const sent: unknown[] = []
  const started = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    expect(request.headers()['authorization']).toBeUndefined()
    switch (new URL(request.url()).pathname) {
      case '/api/organization-plans':
        await route.fulfill({ json: { plans } })
        break
      case '/api/organization-admissions':
        expect(request.method()).toBe('POST')
        sent.push(request.postDataJSON())
        started.resolve()
        await release.promise
        await route.fulfill(sent.length === 1 ? { status: 503, json: {} } : { status: 202, json: { status: 'pending' } })
        break
      default: throw new Error(`Unexpected pricing request: ${request.url()}`)
    }
  })
  try {
    await page.goto('/#pricing')
    const organization = page.getByRole('textbox', { name: 'Organization name', exact: true })
    await organization.fill('Example Organization')
    const email = page.getByRole('textbox', { name: 'Email address' })
    await email.fill('owner@')
    await page.getByRole('button', { name: 'Create organization', exact: true }).click()
    await expect(email).toBeFocused()
    await expect(email).toHaveAttribute('aria-invalid', 'true')
    expect(sent).toEqual([])
    await email.fill('owner@example.test')
    await page.getByRole('radio', { name: 'Pro', exact: true }).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page.getByRole('radio', { name: 'Max', exact: true })).toBeChecked()
    await page.getByRole('button', { name: 'Create organization', exact: true }).click()
    await started.promise
    await expect(email).toBeDisabled()
    await expect(organization).toBeDisabled()
    await expect(page.getByRole('radio', { name: 'Ultra', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Sending request...' })).toBeDisabled()
    release.resolve()
    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toContainText('Please try again')
    await expect(email).toHaveValue('owner@example.test')
    await expect(organization).toHaveValue('Example Organization')
    await expect(page.getByRole('radio', { name: 'Max', exact: true })).toBeChecked()
    await page.getByRole('button', { name: 'Create organization', exact: true }).click()
    await expect(page.getByRole('status').filter({ hasText: 'pending genesis' })).toBeVisible()
    expect(sent).toEqual([
      { organization_name: 'Example Organization', email: 'owner@example.test', tier: 'max' },
      { organization_name: 'Example Organization', email: 'owner@example.test', tier: 'max' },
    ])
    await expect(email).toBeDisabled()
    await page.screenshot({ path: testInfo.outputPath('pending-approval.png') })
  } finally { release.resolve() }
})

test('mobile marketing navigation reaches pricing and retains member login', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.route('**/api/**', async (route) => {
    switch (new URL(route.request().url()).pathname) {
      case '/api/organization-plans': await route.fulfill({ json: { plans } }); break
      case '/api/auth/providers': await route.fulfill({ json: [] }); break
      default: throw new Error(`Unexpected marketing request: ${route.request().url()}`)
    }
  })
  await page.goto('/')
  await page.getByRole('button', { name: 'Open menu' }).click()
  await page.getByRole('navigation', { name: 'Mobile', exact: true }).getByRole('link', { name: 'Pricing', exact: true }).click()
  await expect(page).toHaveURL(/#pricing$/)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('radio', { name: 'Ultra', exact: true })).toBeVisible()
  await expect(page.locator('footer').getByRole('link', { name: 'Pricing', exact: true })).toHaveAttribute('href', '#pricing')
  await expect(page.locator('#cta').getByRole('link', { name: 'View plans' })).toHaveAttribute('href', '#pricing')
  expect(await page.locator('#pricing').evaluate((section) => section.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile-pricing.png') })
  await page.getByRole('button', { name: 'Open menu' }).click()
  await page.getByRole('navigation', { name: 'Mobile', exact: true }).getByRole('link', { name: 'Sign in' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByLabel('Password', { exact: true })).toBeVisible()
})
