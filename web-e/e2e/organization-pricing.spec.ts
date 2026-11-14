import { expect, test } from '@playwright/test'
import type { Page, Route } from '@playwright/test'
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

const recipient = { bank_name: 'Example Bank', account_number: '0123456789', branch_name: 'Example Branch' }
const reference = 'MO7K2QX4ZA'

type OrganizationRequest = { organization_name: string; email: string; phone: string; tier: string; billing: string }

function quoteFor(body: OrganizationRequest) {
  const plan = plans.find(({ id }) => id === body.tier)
  if (plan === undefined) throw new Error(`Unknown tier: ${body.tier}`)
  const amountUsd = body.billing === 'annual' ? plan.annual_price_usd : plan.monthly_price_usd
  return {
    ...body, plan, amount_usd: amountUsd, usd_vnd_rate: 25000, amount_vnd: amountUsd * 25000, recipient, reference,
    expires_at: new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString(), quote_token: 'payload.signature',
  }
}

async function routeOrganizationApi(page: Page, handlers: { quote?: (route: Route) => Promise<void>; admission?: (route: Route) => Promise<void> }) {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    expect(request.headers()['authorization']).toBeUndefined()
    const { quote, admission } = handlers
    switch (new URL(request.url()).pathname) {
      case '/api/organization-plans': await route.fulfill({ json: { plans } }); break
      case '/api/organization-admissions/quote':
        expect(request.method()).toBe('POST')
        if (quote === undefined) throw new Error('Unexpected quote request')
        await quote(route)
        break
      case '/api/organization-admissions':
        expect(request.method()).toBe('POST')
        if (admission === undefined) throw new Error('Unexpected admission request')
        await admission(route)
        break
      default: throw new Error(`Unexpected pricing request: ${request.url()}`)
    }
  })
}

const fulfillQuote = (route: Route) => route.fulfill({ json: quoteFor(route.request().postDataJSON()) })

async function requestQuote(page: Page, { tier = 'Max', annual = true }: { tier?: string; annual?: boolean } = {}) {
  await page.goto('/#pricing')
  await page.getByRole('textbox', { name: 'Organization name', exact: true }).fill('Example Organization')
  await page.getByRole('textbox', { name: 'Email address' }).fill('owner@example.test')
  await page.getByRole('textbox', { name: 'Phone number', exact: true }).fill('0912 345 678')
  await page.getByRole('radio', { name: tier, exact: true }).check()
  if (annual) await page.locator('label').filter({ has: page.getByRole('radio', { name: /^Pay annually/ }) }).click()
  await page.getByRole('button', { name: 'Create organization', exact: true }).click()
}

const pngReceipt = { name: 'receipt.png', mimeType: 'image/png', buffer: Buffer.from('89504e470d0a1a0a', 'hex') }

test('email validation, keyboard tier selection, billing, disabled in-flight controls and retry preserve inputs, then the quote opens the payment page', async ({ page }) => {
  const sent: OrganizationRequest[] = []
  const started = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await routeOrganizationApi(page, {
    quote: async (route) => {
      sent.push(route.request().postDataJSON())
      started.resolve()
      await release.promise
      await (sent.length === 1 ? route.fulfill({ status: 503, json: {} }) : fulfillQuote(route))
    },
  })
  try {
    await page.goto('/#pricing')
    const create = page.getByRole('button', { name: 'Create organization', exact: true })
    const organization = page.getByRole('textbox', { name: 'Organization name', exact: true })
    const email = page.getByRole('textbox', { name: 'Email address' })
    const phone = page.getByRole('textbox', { name: 'Phone number', exact: true })
    const country = page.getByRole('combobox', { name: 'Country code', exact: true })
    const monthly = page.getByRole('radio', { name: 'Pay monthly', exact: true })
    const annually = page.getByRole('radio', { name: /^Pay annually/ })
    await expect(country).toHaveValue('VN')
    await expect(monthly).toBeChecked()
    await expect(create).toBeDisabled()
    await organization.fill('Example Organization')
    await email.fill('owner@')
    await phone.fill('   ')
    await expect(create).toBeDisabled()
    await phone.fill('123')
    await expect(create).toBeEnabled()
    await create.click()
    await expect(email).toBeFocused()
    await expect(email).toHaveAttribute('aria-invalid', 'true')
    expect(sent).toEqual([])
    await email.fill('owner@example.test')
    await create.click()
    await expect(phone).toBeFocused()
    await expect(page.getByRole('alert')).toContainText('Enter a valid phone number.')
    expect(sent).toEqual([])
    await phone.fill('0912 345 678')
    await page.getByRole('radio', { name: 'Pro', exact: true }).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page.getByRole('radio', { name: 'Max', exact: true })).toBeChecked()
    await create.click()
    await started.promise
    await expect(email).toBeDisabled()
    await expect(organization).toBeDisabled()
    await expect(phone).toBeDisabled()
    await expect(country).toBeDisabled()
    await expect(monthly).toBeDisabled()
    await expect(annually).toBeDisabled()
    await expect(page.getByRole('radio', { name: 'Ultra', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Sending request...' })).toBeDisabled()
    release.resolve()
    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toContainText('Please try again')
    await expect(page).toHaveURL(/\/#pricing$/)
    await expect(email).toHaveValue('owner@example.test')
    await expect(organization).toHaveValue('Example Organization')
    await expect(phone).toHaveValue('0912 345 678')
    await expect(page.getByRole('radio', { name: 'Max', exact: true })).toBeChecked()
    await page.locator('label').filter({ has: annually }).click()
    await expect(annually).toBeChecked()
    await create.click()
    await expect(page).toHaveURL(/\/organization-payment$/)
    await expect(page.getByRole('heading', { level: 1, name: 'Pay for your organization' })).toBeVisible()
    expect(sent).toEqual([
      { organization_name: 'Example Organization', email: 'owner@example.test', phone: '+84912345678', tier: 'max', billing: 'monthly' },
      { organization_name: 'Example Organization', email: 'owner@example.test', phone: '+84912345678', tier: 'max', billing: 'annual' },
    ])
  } finally { release.resolve() }
})

test('the quote opens a payment page whose receipt upload sends the request and shows the outcome', async ({ page, context }, testInfo) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  const quoted: OrganizationRequest[] = []
  const uploads: { contentType: string | undefined; body: string }[] = []
  const held = Promise.withResolvers<void>()
  await routeOrganizationApi(page, {
    quote: async (route) => { quoted.push(route.request().postDataJSON()); await fulfillQuote(route) },
    admission: async (route) => {
      const request = route.request()
      uploads.push({ contentType: request.headers()['content-type'], body: request.postDataBuffer()?.toString('latin1') ?? '' })
      await held.promise
      await route.fulfill({ status: 202, json: { status: 'pending', reference } })
    },
  })
  try {
    await requestQuote(page)
    await expect(page).toHaveURL(/\/organization-payment$/)
    expect(quoted).toEqual([{ organization_name: 'Example Organization', email: 'owner@example.test', phone: '+84912345678', tier: 'max', billing: 'annual' }])

    const transfer = page.locator('dl').first()
    await expect(page.getByRole('heading', { name: 'Transfer this amount' })).toBeVisible()
    await expect(transfer.locator('dt', { hasText: 'Amount' }).locator('xpath=following-sibling::dd[1]')).toContainText('62,250,000')
    await expect(transfer).toContainText('Example Bank')
    await expect(transfer).toContainText('0123456789')
    await expect(transfer).toContainText('Example Branch')
    await expect(transfer.locator('dt', { hasText: 'Transfer note' }).locator('xpath=following-sibling::dd[1]')).toContainText(reference)
    await expect(page.getByText('$2,490 at 25,000 VND per USD')).toBeVisible()
    await expect(page.getByText(`Write ${reference} in the transfer note so we can match your payment.`)).toBeVisible()
    await expect(page.getByText(/^This price is valid until /)).toBeVisible()
    await expect(page.getByRole('link', { name: 'Back to pricing' })).toHaveAttribute('href', '/#pricing')
    await expect(page.locator('header').getByRole('link', { name: 'Pricing', exact: true })).toHaveAttribute('href', '/#pricing')

    await page.getByRole('button', { name: 'Copy Amount' }).click()
    await expect(page.getByRole('status').filter({ hasText: 'Amount copied' })).toBeAttached()
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('62250000')

    const summary = page.getByRole('heading', { name: 'Request summary' }).locator('xpath=ancestor::div[@data-slot="card"][1]')
    for (const text of ['Example Organization', 'owner@example.test', '+84 912 345 678', 'Max', 'Annual', '$2,490', '25,000 VND per USD', '62,250,000']) {
      await expect(summary).toContainText(text)
    }

    const submit = page.getByRole('button', { name: 'Submit request', exact: true })
    const file = page.getByLabel('Payment receipt', { exact: true })
    await expect(submit).toBeDisabled()
    await file.setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') })
    await expect(page.getByRole('alert')).toContainText('Choose a PNG, JPEG, WebP or PDF file.')
    await expect(submit).toBeDisabled()
    await file.setInputFiles(pngReceipt)
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByText('Selected: receipt.png')).toBeVisible()
    await expect(submit).toBeEnabled()
    await page.screenshot({ path: testInfo.outputPath('payment-desktop.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: testInfo.outputPath('payment-mobile.png'), fullPage: true })
    await page.setViewportSize({ width: 1280, height: 720 })

    await submit.click()
    await expect(page.getByRole('button', { name: 'Sending...' })).toBeDisabled()
    await expect(file).toBeDisabled()
    held.resolve()
    const outcome = page.getByRole('heading', { name: 'Request received' })
    await expect(outcome).toBeFocused()
    await expect(page.getByRole('status').filter({ hasText: `reference ${reference}` })).toContainText('owner@example.test')
    await expect(page.getByRole('heading', { name: 'Transfer this amount' })).toHaveCount(0)
    await expect(submit).toHaveCount(0)
    expect(await page.evaluate(() => sessionStorage.getItem('maraclaw-organization-quote'))).toBeNull()
    expect(uploads).toHaveLength(1)
    expect(uploads[0]?.contentType).toMatch(/^multipart\/form-data; boundary=/)
    expect(uploads[0]?.body).toContain('name="quote_token"')
    expect(uploads[0]?.body).toContain('payload.signature')
    expect(uploads[0]?.body).toContain('filename="receipt.png"')
    await page.screenshot({ path: testInfo.outputPath('payment-outcome.png') })
  } finally { held.resolve() }
})

test('a refused quote shows its fixed message inline and keeps the form on the landing page', async ({ page }) => {
  const codes = ['email_registered', 'admission_already_pending', 'domain_claimed', 'payments_not_configured']
  const messages = [
    'This email address already has an account. Sign in to continue.',
    'A request for this email address is already waiting for review.',
    'This email domain already belongs to another organization. Use a different email address.',
    'Organization requests are temporarily unavailable. Try again later.',
  ]
  let attempt = 0
  await routeOrganizationApi(page, {
    quote: async (route) => {
      await route.fulfill({ status: 409, json: { detail: { code: codes[attempt], message: 'Engine message.' } } })
      attempt += 1
    },
  })
  await requestQuote(page)
  const alert = page.getByRole('alert')
  for (const message of messages) {
    await expect(alert).toHaveText(message)
    await expect(page).toHaveURL(/\/#pricing$/)
    await expect(page.getByRole('textbox', { name: 'Email address' })).toHaveValue('owner@example.test')
    await page.getByRole('button', { name: 'Create organization', exact: true }).click()
  }
  await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
})

test('the payment page without a usable quote offers to start again', async ({ page }) => {
  await routeOrganizationApi(page, {})
  await page.goto('/organization-payment')
  await expect(page.getByRole('heading', { name: 'Start your request again' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Start again' })).toHaveAttribute('href', '/#pricing')
  await expect(page.getByRole('heading', { name: 'Transfer this amount' })).toHaveCount(0)
  const expired = { ...quoteFor({ organization_name: 'Example Organization', email: 'owner@example.test', phone: '+84912345678', tier: 'max', billing: 'monthly' }), expires_at: '2020-01-01T00:00:00Z' }
  await page.evaluate((value) => sessionStorage.setItem('maraclaw-organization-quote', JSON.stringify(value)), expired)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Start your request again' })).toBeVisible()
  expect(await page.evaluate(() => sessionStorage.getItem('maraclaw-organization-quote'))).toBeNull()
})

test('refused receipts show fixed messages, and an expired quote sends the visitor back to pricing', async ({ page }) => {
  const refusals = [
    { status: 409, json: { detail: { code: 'email_registered', message: 'Engine message.' } }, message: 'This email address already has an account. Sign in to continue.' },
    { status: 409, json: { detail: { code: 'admission_already_pending', message: 'Engine message.' } }, message: 'A request for this email address is already waiting for review.' },
    { status: 409, json: { detail: { code: 'domain_claimed', message: 'Engine message.' } }, message: 'This email domain already belongs to another organization. Use a different email address.' },
    { status: 409, json: { detail: { code: 'quote_already_used', message: 'Engine message.' } }, message: 'This price quote was already used for an earlier request. Start again from the pricing section to get a new one.' },
    { status: 413, json: { detail: 'Too large.' }, message: 'This file is larger than 10 MiB. Choose a smaller file.' },
    { status: 415, json: { detail: 'Unsupported.' }, message: 'Choose a PNG, JPEG, WebP or PDF file.' },
    { status: 429, json: { detail: { code: 'too_many_requests', message: 'Engine message.' } }, message: 'Too many requests. Try again later.' },
    { status: 503, json: { detail: 'No reviewer.' }, message: 'We could not send your request. Please try again.' },
    { status: 410, json: { detail: { code: 'quote_expired', message: 'Engine message.' } }, message: '' },
  ]
  let attempt = 0
  await routeOrganizationApi(page, {
    quote: fulfillQuote,
    admission: async (route) => {
      const refusal = refusals[attempt]
      attempt += 1
      await route.fulfill({ status: refusal?.status ?? 500, json: refusal?.json ?? {} })
    },
  })
  await requestQuote(page, { tier: 'Pro', annual: false })
  await expect(page).toHaveURL(/\/organization-payment$/)
  await page.getByLabel('Payment receipt', { exact: true }).setInputFiles(pngReceipt)
  const submit = page.getByRole('button', { name: 'Submit request', exact: true })
  for (const { message } of refusals.slice(0, -1)) {
    await submit.click()
    await expect(page.getByRole('alert')).toHaveText(message)
    await expect(page.getByRole('heading', { name: 'Transfer this amount' })).toBeVisible()
    await expect(submit).toBeEnabled()
  }
  await submit.click()
  const restart = page.getByRole('heading', { name: 'Start your request again' })
  await expect(restart).toBeFocused()
  await expect(page.getByRole('status')).toContainText('This price quote is no longer valid.')
  await expect(page.getByRole('link', { name: 'Start again' })).toHaveAttribute('href', '/#pricing')
  expect(await page.evaluate(() => sessionStorage.getItem('maraclaw-organization-quote'))).toBeNull()
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
  await page.getByRole('textbox', { name: 'Phone number', exact: true }).scrollIntoViewIfNeeded()
  await expect(page.getByRole('combobox', { name: 'Country code', exact: true })).toHaveValue('VN')
  await page.screenshot({ path: testInfo.outputPath('mobile-pricing.png') })
  await page.getByRole('button', { name: 'Open menu' }).click()
  await page.getByRole('navigation', { name: 'Mobile', exact: true }).getByRole('link', { name: 'Sign in' }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByLabel('Password', { exact: true })).toBeVisible()
})
