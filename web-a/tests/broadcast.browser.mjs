import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { chromium, devices, expect } from '@playwright/test'
import { createServer } from 'vite'
import { assertReducedPress } from './company-deletion-browser-evidence.mjs'

const output = process.env.BROADCAST_ARTIFACTS ?? join(process.env.TMPDIR, 'broadcast-evidence')
await mkdir(output, { recursive: true })
const server = await createServer({ server: { host: '127.0.0.1', port: 0, watch: { ignored: (path) => path.startsWith(output) } } })
await server.listen()
const base = server.resolvedUrls.local[0]
const counts = { ok: true, users_notified: 7, agents_notified: 3, emails_sent: 5 }
const details = { 400: 'System email is not configured. Please configure it in Platform Settings.', 403: 'Only org admins can send broadcasts', 503: 'Synthetic service unavailable' }
const admin = {
  id: 'operator', identity_id: 'identity', username: 'operator', email: 'operator@example.test', display_name: 'Operator',
  avatar_url: null, role: 'org_admin', is_platform_admin: false, tenant_id: 'tenant-a', title: null, primary_mobile: null,
  registration_source: null, is_active: true, email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}
const evidence = []
let closedContexts = 0
let closedBrowsers = 0
let completed = false

async function scenario(options, exercise) {
  const browser = await chromium.launch({ headless: true })
  let context
  const state = { user: { ...admin, ...options.user }, token: 'synthetic-token', status: 200, gate: null, exchanges: [] }
  const errors = []
  try {
    context = await browser.newContext({ ...(options.mobile ? devices['iPhone 13'] : { viewport: { width: 1365, height: 1100 } }), colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce' })
    await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-token'))
    const page = await context.newPage()
    page.on('pageerror', (error) => errors.push(error.message))
    await page.route('**/api/**', async (route) => {
      const path = new URL(route.request().url()).pathname
      let body
      let status = 200
      let exchange
      if (path === '/api/auth/me') body = state.user
      else if (path === '/api/auth/login') body = { access_token: state.token, token_type: 'bearer', user: state.user }
      else if (path === '/api/admin/search-analytics/summary') body = { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' }
      else if (path === '/api/admin/search-analytics/orgs') body = []
      else if (path === '/api/notifications/broadcast' && route.request().method() === 'POST') {
        exchange = { payload: route.request().postDataJSON(), status: state.status, gate: state.gate, delivered: Promise.withResolvers() }
        state.exchanges.push(exchange)
        if (exchange.gate) await exchange.gate.promise
        status = exchange.status
        body = status === 200 ? counts : { detail: details[status] }
      } else throw new Error(`Unexpected synthetic API ${route.request().method()} ${path}`)
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
      exchange?.delivered.resolve()
    })
    await page.goto(`${base}broadcast`)
    await exercise(page, state)
    assert.deepEqual(errors, [])
    evidence.push(options.name)
    console.log(`PASS ${options.name}`)
  } finally {
    for (const exchange of state.exchanges) exchange.gate?.resolve()
    await context?.close()
    closedContexts++
    await browser.close()
    closedBrowsers++
  }
}

async function compose(page, draft = { title: '  Maintenance  ', body: 'First line\nSecond line', send_email: true }) {
  await page.getByLabel('Title', { exact: true }).fill(draft.title)
  await page.getByLabel('Body (optional)', { exact: true }).fill(draft.body)
  await page.getByLabel('Also send email', { exact: true }).setChecked(draft.send_email)
  await page.getByRole('button', { name: 'Review broadcast', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Review broadcast' })).toBeFocused()
}

async function capture(page, name) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  await page.screenshot({ path: join(output, `${name}.png`), animations: 'disabled' })
}

async function flush(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))))
}

try {
  for (const mobile of [false, true]) for (const [role, platform, target] of [
    ['org_admin', false, 'broadcast'], ['platform_admin', true, ''], ['platform_admin', false, ''],
    ['org_admin', true, ''], ['member', false, 'login'],
  ]) await scenario({ name: `${mobile ? 'mobile' : 'desktop'} access ${role}/${platform}`, mobile, user: { role, is_platform_admin: platform } }, async (page, state) => {
    await expect(page).toHaveURL(`${base}${target}`)
    const links = page.getByRole('link', { name: 'Broadcast', exact: true })
    if (target === 'broadcast') {
      await expect(links).toHaveCount(1)
      await expect(links).toHaveAttribute('aria-current', 'page')
      await page.getByRole('link', { name: 'Account', exact: true }).click()
      await links.click()
      await expect(page.getByRole('heading', { name: 'Organization broadcast' })).toBeVisible()
    } else {
      await expect(links).toHaveCount(0)
      await expect(page.getByRole('heading', { name: 'Organization broadcast' })).toHaveCount(0)
    }
    assert.equal(state.exchanges.length, 0)
  })

  for (const mobile of [false, true]) await scenario({ name: `${mobile ? 'mobile' : 'desktop'} force-password blocks direct route and navigation`, mobile, user: { must_change_password: true } }, async (page, state) => {
    await expect(page).toHaveURL(`${base}account`)
    await expect(page.getByRole('link', { name: 'Broadcast', exact: true })).toHaveCount(0)
    await expect(page.locator('[aria-disabled="true"]:visible').filter({ hasText: /^Broadcast$/ })).toHaveCount(1)
    await expect(page.getByRole('heading', { name: 'Organization broadcast' })).toHaveCount(0)
    assert.equal(state.exchanges.length, 0)
  })

  await scenario({ name: 'required title, length boundaries, email off and no Enter send' }, async (page, state) => {
    const title = page.getByLabel('Title', { exact: true })
    const body = page.getByLabel('Body (optional)', { exact: true })
    const review = page.getByRole('button', { name: 'Review broadcast', exact: true })
    await expect(page.getByLabel('Also send email')).not.toBeChecked()
    for (const value of ['', '   ', 'a'.repeat(201)]) {
      await title.fill(value); await review.click()
      await expect(title).toHaveAttribute('aria-invalid', 'true')
      await expect(title).toBeFocused()
      await expect(page.getByRole('heading', { name: 'Review broadcast' })).toHaveCount(0)
    }
    await title.fill('a'.repeat(200)); await body.fill('b'.repeat(1001)); await review.click()
    await expect(body).toHaveAttribute('aria-invalid', 'true')
    await expect(body).toBeFocused()
    await body.fill(''); await title.press('Enter')
    const heading = page.getByRole('heading', { name: 'Review broadcast' })
    await expect(heading).toBeFocused(); await heading.press('Enter'); await flush(page)
    assert.equal(state.exchanges.length, 0)
    await heading.press('Tab'); await expect(page.getByRole('button', { name: 'Back to edit' })).toBeFocused()
    await page.keyboard.press('Enter'); await expect(title).toBeFocused()
    await expect(title).toHaveValue('a'.repeat(200)); await expect(body).toHaveValue('')
  })

  for (const mobile of [false, true]) for (const scheme of ['light', 'dark']) await scenario({ name: `${mobile ? 'mobile' : 'desktop'} ${scheme} exact snapshot, pending lock, recovery and counts`, mobile, scheme }, async (page, state) => {
    const prefix = `${mobile ? 'mobile' : 'desktop'}-${scheme}`
    await expect(page.getByLabel('Also send email')).not.toBeChecked()
    await capture(page, `${prefix}-compose`)
    const draft = { title: '  Operations update  ', body: 'Next steps:\nKeep this exact spacing.\n\nThank you.', send_email: true }
    await compose(page, draft)
    const region = page.getByRole('region', { name: 'Review broadcast' })
    assert.equal(await region.locator('dd').nth(0).textContent(), draft.title)
    assert.equal(await region.locator('dd').nth(1).textContent(), draft.body)
    await expect(region).toContainText('Active users in your current organization, except you, plus all organization agents.')
    await expect(page.getByRole('textbox')).toHaveCount(0)
    await capture(page, `${prefix}-review-focus`)
    await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(page.getByLabel('Title', { exact: true })).toBeFocused()
    await expect(page.getByLabel('Body (optional)')).toHaveValue(draft.body)
    await compose(page, draft)
    state.status = 400; state.gate = Promise.withResolvers()
    const send = page.getByRole('button', { name: 'Send broadcast', exact: true })
    await assertReducedPress(page, send, join(output, `${prefix}-reduced-motion-pressed.png`))
    await send.evaluate((button) => { button.click(); button.click() })
    await expect.poll(() => state.exchanges.length).toBe(1)
    await expect(page.getByRole('button', { name: 'Sending broadcast...' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Back to edit' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled()
    await page.clock.install(); await page.clock.fastForward(60_000)
    assert.equal(state.exchanges.length, 1)
    await capture(page, `${prefix}-pending`)
    state.gate.resolve()
    await expect(page.getByRole('alert')).toContainText(details[400])
    await expect(page.getByRole('alert')).toBeFocused()
    await capture(page, `${prefix}-error`)
    await expect(page.getByLabel('Title', { exact: true })).toHaveValue(draft.title)
    await expect(page.getByLabel('Body (optional)')).toHaveValue(draft.body)
    await expect(page.getByLabel('Also send email')).toBeChecked()
    await expect(page.getByRole('button', { name: 'Send broadcast', exact: true })).toHaveCount(0)
    await page.clock.fastForward(60_000)
    assert.equal(state.exchanges.length, 1)
    assert.deepEqual(state.exchanges[0].payload, draft)
    state.status = 200; state.gate = null
    await compose(page, draft); await page.getByRole('button', { name: 'Send broadcast', exact: true }).press('Enter')
    await expect(page.getByRole('heading', { name: 'Broadcast sent' })).toBeVisible()
    for (const [label, count] of [['Users notified', '7'], ['Agents notified', '3'], ['Queued email targets', '5']]) await expect(page.locator('dl > div').filter({ has: page.getByText(label, { exact: true }) }).locator('dd')).toHaveText(count)
    await expect(page.getByRole('status').filter({ has: page.getByRole('heading', { name: 'Broadcast sent' }) })).toBeFocused()
    await capture(page, `${prefix}-success`)
    await expect(page.getByText('Email targets are queued for processing, not confirmed deliveries.')).toBeVisible()
    await expect(page.getByLabel('Title', { exact: true })).toHaveValue('')
    await expect(page.getByLabel('Also send email')).not.toBeChecked()
    assert.equal(state.exchanges.length, 2)
  })

  for (const status of [403, 503]) await scenario({ name: `HTTP ${status} preserves draft and never retries` }, async (page, state) => {
    state.status = status
    await compose(page); await page.getByRole('button', { name: 'Send broadcast', exact: true }).click()
    await expect(page.getByRole('alert')).toContainText(details[status])
    await expect(page.getByLabel('Title', { exact: true })).toHaveValue('  Maintenance  ')
    await expect(page.getByLabel('Body (optional)')).toHaveValue('First line\nSecond line')
    await expect(page.getByLabel('Also send email')).toBeChecked()
    await page.clock.install(); await page.clock.fastForward(60_000)
    assert.equal(state.exchanges.length, 1)
    await expect(page.getByRole('button', { name: 'Send broadcast', exact: true })).toHaveCount(0)
    await capture(page, `error-${status}`)
  })

  for (const sameToken of [false, true]) for (const status of [200, 403]) for (const mode of ['draft', 'review', 'pending']) await scenario({ name: `late ${status} after ${sameToken ? 'same-token' : 'different-token'} replacement cannot affect new ${mode}` }, async (page, state) => {
    state.status = status; state.gate = Promise.withResolvers()
    await compose(page); await page.getByRole('button', { name: 'Send broadcast', exact: true }).click()
    await expect.poll(() => state.exchanges.length).toBe(1)
    state.user = { ...admin, id: 'replacement', tenant_id: 'tenant-b' }
    if (!sameToken) state.token = 'synthetic-next-token'
    await page.getByRole('button', { name: 'Sign out', exact: true }).click()
    await page.getByLabel('Email, phone, or username').fill('replacement@example.test')
    await page.getByLabel('Password', { exact: true }).fill('synthetic-password')
    await page.getByRole('button', { name: 'Sign in', exact: true }).click()
    const title = page.getByLabel('Title', { exact: true })
    await title.fill('New owner draft'); await page.getByLabel('Body (optional)').fill('Keep my new content')
    const draft = { title: 'New owner draft', body: 'Keep my new content', send_email: false }
    state.status = 200; state.gate = mode === 'pending' ? Promise.withResolvers() : null
    if (mode !== 'draft') await compose(page, draft)
    if (mode === 'pending') {
      await page.getByRole('button', { name: 'Send broadcast', exact: true }).click()
      await expect.poll(() => state.exchanges.length).toBe(2)
    }
    state.exchanges[0].gate.resolve(); await state.exchanges[0].delivered.promise; await flush(page)
    if (mode === 'draft') {
      await expect(title).toHaveValue(draft.title)
      await expect(page.getByLabel('Body (optional)')).toHaveValue(draft.body)
      await expect(page.locator('dl')).toHaveCount(0)
    } else {
      await expect(page.getByRole('heading', { name: 'Review broadcast' })).toBeVisible()
      assert.equal(await page.locator('dd').nth(1).textContent(), draft.body)
      if (mode === 'pending') await expect(page.getByRole('button', { name: 'Sending broadcast...' })).toBeDisabled()
    }
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'Broadcast sent' })).toHaveCount(0)
    await expect(page.locator('[data-sonner-toast]').filter({ hasNotText: /^Signed in$/ })).toHaveCount(0)
    await capture(page, `replacement-${sameToken}-${status}-clean-${mode}`)
    if (mode === 'draft') await compose(page, draft)
    if (mode === 'pending') state.gate.resolve()
    else await page.getByRole('button', { name: 'Send broadcast', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Broadcast sent' })).toBeVisible()
    assert.equal(state.exchanges.length, 2)
  })

  await scenario({ name: 'long content, narrow reflow and scroll preserve keyboard controls', mobile: true, scheme: 'dark' }, async (page, state) => {
    const draft = { title: 'LongTitleWithoutBreaks'.repeat(9), body: 'LongBodyWithoutBreaks'.repeat(45), send_email: false }
    await compose(page, draft)
    await capture(page, 'long-review-mobile-top')
    const back = page.getByRole('button', { name: 'Back to edit' })
    await back.focus(); await capture(page, 'long-review-mobile-scrolled-focus')
    assert.equal(await page.locator('main').evaluate((element) => element.scrollWidth <= element.clientWidth), true)
    await page.setViewportSize({ width: 320, height: 640 }); await back.scrollIntoViewIfNeeded()
    await capture(page, 'long-review-320px')
    await page.keyboard.press('Escape'); await expect(page.getByLabel('Title', { exact: true })).toBeFocused()
    await expect(page.getByLabel('Body (optional)')).toHaveValue(draft.body)
    assert.equal(state.exchanges.length, 0)
  })
  completed = true
} finally {
  await server.close()
  const receipt = { completed, passed: evidence.length, scenarios: evidence, output, cleanup: { closedContexts, closedBrowsers, viteClosed: true, syntheticAPIsOnly: true } }
  await writeFile(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2))
  console.log(JSON.stringify(receipt))
}
