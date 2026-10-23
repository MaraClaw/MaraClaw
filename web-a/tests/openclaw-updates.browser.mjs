import assert from 'node:assert/strict'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { chromium, devices, expect } from '@playwright/test'
import { z } from 'zod'
import { createServer } from 'vite'

const output = await mkdtemp(join(tmpdir(), 'openclaw-updates-'))
const server = await createServer({ server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const base = server.resolvedUrls.local[0]
const admin = {
  id: 'operator', identity_id: 'identity', email: 'operator@example.test', display_name: 'Operator',
  avatar_url: null, role: 'platform_admin', is_platform_admin: true, tenant_id: null, title: null, primary_mobile: null,
  registration_source: null, is_active: true, email_verified: true, must_change_password: false, created_at: '2026-10-20T00:00:00Z',
}
const evidence = []

async function scenario(options, exercise) {
  if (process.env.OPENCLAW_BROWSER_CASE && !new RegExp(process.env.OPENCLAW_BROWSER_CASE).test(options.name)) return
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ ...(options.mobile ? devices['iPhone 13'] : { viewport: { width: options.width ?? 1440, height: 1080 } }), colorScheme: options.scheme ?? 'light', reducedMotion: 'reduce' })
  context.setDefaultTimeout(10_000)
  context.setDefaultNavigationTimeout(10_000)
  const state = { user: { ...admin, ...options.user }, status: 201, gate: null, exchanges: [] }
  const errors = []
  try {
    await context.addInitScript(() => localStorage.setItem('maraclaw-admin-token', 'synthetic-token'))
    const page = await context.newPage()
    page.on('pageerror', (error) => errors.push(error.message))
    await page.route('**/api/**', async (route) => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/admin/openclaw-updates') {
        const exchange = { payload: route.request().postDataJSON(), status: state.status, gate: state.gate }
        state.exchanges.push(exchange)
        if (exchange.gate) await exchange.gate.promise
        if (exchange.status === 'offline') { await route.abort('connectionfailed'); return }
        const body = exchange.status >= 400 ? { detail: `Synthetic ${exchange.status}` } : {
          notice: { id: 'bf38d6d1-92a9-4308-81c2-9b5942ab80c1', title: exchange.payload.title, body: exchange.payload.body, mandatory: exchange.payload.mandatory, created_at: '2026-10-22T10:00:00Z' }, recipients: 17,
        }
        await route.fulfill({ status: exchange.status, json: body })
        return
      }
      const body = path === '/api/auth/me' ? state.user
        : path === '/api/notifications/unread-count' ? { unread_count: 0 }
          : path === '/api/admin/search-analytics/summary' ? { event_count: 0, error_count: 0, quota_count: 0, unique_orgs: 0, unique_agents: 0, unattributed_count: 0, avg_latency_ms: 0, by_kind: [], scope: 'system' }
            : path === '/api/admin/search-analytics/orgs' ? [] : null
      if (body === null) throw new Error(`Unexpected synthetic API ${route.request().method()} ${path}`)
      await route.fulfill({ json: body })
    })
    await page.goto(`${base}openclaw-updates`)
    await exercise(page, state)
    assert.deepEqual(errors, [])
    evidence.push(options.name)
    console.log(`PASS ${options.name}`)
  } catch (error) {
    console.error({ scenario: options.name, pageErrors: errors, browserConnected: browser.isConnected() })
    throw error
  } finally {
    for (const exchange of state.exchanges) exchange.gate?.resolve()
    await context.close()
    await browser.close()
  }
}

async function compose(page, draft = { title: '  Release  ', body: 'Upgrade details\nSecond line', mandatory: false }) {
  await page.getByLabel('Title', { exact: true }).fill(draft.title)
  await page.getByLabel('Body (optional)', { exact: true }).fill(draft.body)
  await page.getByRole('radio', { name: draft.mandatory ? /^Mandatory/ : /^Optional/ }).check()
  await page.getByRole('button', { name: 'Review update notice', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Review update notice' })).toBeFocused()
}

async function capture(page, name, action) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
  assert.equal(await page.locator('main').evaluate((element) => element.scrollWidth <= element.clientWidth), true)
  await page.screenshot({ path: join(output, `${name}.png`), animations: 'disabled' })
  if (action && page.viewportSize().width === 390) {
    const main = page.locator('main')
    const top = await main.evaluate((element) => element.scrollTop)
    await action.scrollIntoViewIfNeeded()
    await page.screenshot({ path: join(output, `${name}-actions.png`), animations: 'disabled' })
    await main.evaluate((element, position) => { element.scrollTop = position }, top)
  }
}

try {
  for (const mobile of [false, true]) for (const [role, platform, target] of [
    ['platform_admin', true, 'openclaw-updates'], ['platform_admin', false, 'openclaw-updates'], ['org_admin', false, ''],
    ['org_admin', true, 'openclaw-updates'], ['member', false, 'login'],
  ]) await scenario({ name: `access ${mobile}/${role}/${platform}`, mobile, user: { role, is_platform_admin: platform } }, async (page, state) => {
    // Given / When: direct navigation in the role's real app session.
    await expect(page).toHaveURL(`${base}${target}`)
    // Then: both route and navigation enforce PA identity.
    const links = page.getByRole('link', { name: 'Updates', exact: true })
    await expect(links).toHaveCount(target === 'openclaw-updates' ? 1 : 0)
    if (target === 'openclaw-updates') {
      await expect(links).toHaveAttribute('aria-current', 'page')
      await page.getByRole('link', { name: 'Account', exact: true }).click()
      await links.click()
      await expect(page).toHaveURL(`${base}openclaw-updates`)
    }
    assert.equal(state.exchanges.length, 0)
  })

  for (const mobile of [false, true]) await scenario({ name: `password guard ${mobile}`, mobile, user: { must_change_password: true } }, async (page, state) => {
    // Given / When: a PA with a required password change navigates directly.
    // Then
    await expect(page).toHaveURL(`${base}account`)
    await expect(page.getByRole('link', { name: 'Updates', exact: true })).toHaveCount(0)
    assert.equal(state.exchanges.length, 0)
  })

  await scenario({ name: 'form boundaries, keyboard review, and preserved edits' }, async (page, state) => {
    // Given
    const title = page.getByLabel('Title', { exact: true })
    const body = page.getByLabel('Body (optional)', { exact: true })
    const review = page.getByRole('button', { name: 'Review update notice', exact: true })
    await expect(page.getByRole('radio', { name: /^Optional/ })).toBeChecked()
    // When / Then: invalid fields block the only transition that can lead to sending.
    for (const value of ['', '   ', 'a'.repeat(201)]) {
      await title.fill(value); await review.click()
      await expect(title).toHaveAttribute('aria-invalid', 'true'); await expect(title).toBeFocused()
    }
    await title.fill('a'.repeat(200)); await body.fill('b'.repeat(1001)); await review.click()
    await expect(body).toHaveAttribute('aria-invalid', 'true'); await expect(body).toBeFocused()
    await body.fill('b'.repeat(1000)); await title.press('Enter')
    const heading = page.getByRole('heading', { name: 'Review update notice' })
    await expect(heading).toBeFocused(); await heading.press('Enter')
    assert.equal(state.exchanges.length, 0)
    await page.getByRole('button', { name: 'Back to edit' }).click()
    await expect(title).toBeFocused(); await expect(body).toHaveValue('b'.repeat(1000))
  })

  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) await scenario({ name: `safe retry ${width}/${scheme}`, mobile: width === 390, width, scheme }, async (page, state) => {
    // Given
    const prefix = `${width}-${scheme}`
    await capture(page, `${prefix}-compose`, page.getByRole('button', { name: 'Review update notice', exact: true }))
    const draft = { title: '  New OpenClaw  ', body: 'First line\nSecond line', mandatory: scheme === 'dark' }
    await compose(page, draft)
    const review = page.getByRole('region', { name: 'Review update notice' })
    assert.equal(await review.locator('dd').nth(0).textContent(), draft.title)
    assert.equal(await review.locator('dd').nth(1).textContent(), draft.body)
    await capture(page, `${prefix}-review`, page.getByRole('button', { name: 'Send update notice', exact: true }))
    state.status = 'offline'; state.gate = Promise.withResolvers()
    // When: a double-click races the pending render and the response is lost.
    await page.getByRole('button', { name: 'Send update notice', exact: true }).evaluate((button) => { button.click(); button.click() })
    await expect.poll(() => state.exchanges.length).toBe(1)
    await expect(page.getByRole('button', { name: 'Sending notice…' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Back to edit' })).toBeDisabled()
    await capture(page, `${prefix}-pending`, page.getByRole('button', { name: 'Sending notice…' }))
    state.gate.resolve()
    // Then: the exact request survives the uncertain outcome and retries as a replay.
    await expect(page.getByRole('alert')).toBeFocused()
    await capture(page, `${prefix}-error`, page.getByRole('button', { name: 'Retry this notice', exact: true }))
    await page.clock.install(); await page.clock.fastForward(60_000)
    assert.equal(state.exchanges.length, 1)
    assert.equal(z.uuid().safeParse(state.exchanges[0].payload.request_id).success, true)
    assert.deepEqual(state.exchanges[0].payload, { ...draft, request_id: state.exchanges[0].payload.request_id })
    state.status = 200; state.gate = null
    await page.getByRole('button', { name: 'Retry this notice', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Notice sent' })).toBeVisible()
    assert.deepEqual(state.exchanges[1].payload, state.exchanges[0].payload)
    const status = page.getByRole('status').filter({ has: page.getByRole('heading', { name: 'Notice sent' }) })
    await expect(status).toBeFocused()
    await expect(status.locator('dd').nth(0)).toHaveText('17')
    await expect(status.locator('dd').nth(1)).toHaveText(draft.title)
    await expect(status.locator('dd').nth(2)).toHaveText(draft.body)
    await expect(page.getByLabel('Title', { exact: true })).toHaveValue('')
    await expect(page.getByRole('radio', { name: /^Optional/ })).toBeChecked()
    await capture(page, `${prefix}-success`, status)
    await compose(page, draft); state.status = 201
    await page.getByRole('button', { name: 'Send update notice', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Notice sent' })).toBeVisible()
    assert.notEqual(state.exchanges[2].payload.request_id, state.exchanges[0].payload.request_id)
  })

  await scenario({ name: 'uncertain request survives navigation, reload and admin replacement' }, async (page, state) => {
    // Given: the first send may have reached the server before its response was lost.
    state.status = 'offline'
    await compose(page)
    await page.getByRole('button', { name: 'Send update notice', exact: true }).click()
    await expect(page.getByRole('alert')).toBeFocused()
    const original = state.exchanges[0].payload
    // When: the operator navigates away, reloads, and temporarily uses another account.
    await page.getByRole('link', { name: 'Account', exact: true }).click()
    await page.getByRole('link', { name: 'Updates', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Retry this notice', exact: true })).toBeVisible()
    await page.reload()
    await expect(page.getByRole('button', { name: 'Retry this notice', exact: true })).toBeVisible()
    state.user = { ...admin, id: 'other-operator' }
    await page.reload()
    await expect(page.getByLabel('Title', { exact: true })).toHaveValue('')
    state.user = { ...admin }
    await page.reload()
    // Then: only the original admin can explicitly replay the exact pending payload.
    await expect(page.getByRole('button', { name: 'Retry this notice', exact: true })).toBeVisible()
    assert.equal(state.exchanges.length, 1)
    state.status = 200
    await page.getByRole('button', { name: 'Retry this notice', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Notice sent' })).toBeVisible()
    assert.deepEqual(state.exchanges[1].payload, original)
    await page.reload()
    await expect(page.getByLabel('Title', { exact: true })).toHaveValue('')
    await expect(page.getByRole('button', { name: 'Retry this notice', exact: true })).toHaveCount(0)
  })

  await scenario({ name: 'storage failure blocks sending until the request can be retained' }, async (page, state) => {
    // Given
    await compose(page)
    // When
    await page.getByRole('button', { name: 'Send update notice', exact: true }).evaluate((button) => {
      const original = Storage.prototype.setItem
      try {
        Storage.prototype.setItem = () => { throw new DOMException('Storage unavailable', 'QuotaExceededError') }
        button.click()
      } finally {
        Storage.prototype.setItem = original
      }
    })
    // Then
    await expect(page.getByRole('alert')).toBeFocused()
    assert.equal(state.exchanges.length, 0)
    await expect(page.getByRole('button', { name: 'Retry this notice', exact: true })).toBeEnabled()
  })

  for (const scheme of ['light', 'dark']) await scenario({ name: `unavailable storage on mount/${scheme}`, mobile: true, scheme }, async (page, state) => {
    // Given
    await page.addInitScript(() => {
      Object.defineProperty(window, 'sessionStorage', { get() { throw new DOMException('Storage unavailable', 'SecurityError') } })
    })
    // When
    await page.reload()
    // Then: fail closed without advertising an action that requires inaccessible storage.
    await expect(page.getByRole('alert')).toBeVisible()
    await expect(page.getByLabel('Title', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Compose a new notice', exact: true })).toHaveCount(0)
    assert.equal(state.exchanges.length, 0)
    await capture(page, `390-${scheme}-storage-unavailable`)
  })

  for (const saved of ['{', '{"title":"Incomplete saved request"}']) await scenario({ name: `corrupt storage explicit recovery/${saved}` }, async (page, state) => {
    // Given: accessible storage contains an unreadable request, which must not be silently discarded.
    await page.evaluate((value) => sessionStorage.setItem('maraclaw-admin-openclaw-update:operator', value), saved)
    await page.reload()
    await expect(page.getByRole('alert')).toBeVisible()
    await expect(page.getByLabel('Title', { exact: true })).toHaveCount(0)
    assert.equal(state.exchanges.length, 0)
    // When: the operator explicitly discards the corrupt request.
    await page.getByRole('button', { name: 'Compose a new notice', exact: true }).click()
    // Then: composing and saving a separate notice works again.
    await expect(page.getByLabel('Title', { exact: true })).toBeFocused()
    await compose(page)
    await page.getByRole('button', { name: 'Send update notice', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Notice sent' })).toBeVisible()
    assert.equal(state.exchanges.length, 1)
  })

  for (const status of [403, 409, 422, 503]) await scenario({ name: `HTTP ${status} explicit new submission` }, async (page, state) => {
    // Given
    state.status = status
    await compose(page)
    // When
    await page.getByRole('button', { name: 'Send update notice', exact: true }).click()
    // Then: errors retain review; a deliberate new notice gets a different ID.
    await expect(page.getByRole('alert')).toBeFocused()
    await expect(page.getByRole('region', { name: 'Review update notice' })).toBeVisible()
    await page.getByRole('button', { name: 'Compose a new notice', exact: true }).click()
    await expect(page.getByLabel('Title', { exact: true })).toBeFocused()
    await compose(page, { title: 'Changed title', body: '', mandatory: true }); state.status = 201
    await page.getByRole('button', { name: 'Send update notice', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Notice sent' })).toBeVisible()
    assert.notEqual(state.exchanges[1].payload.request_id, state.exchanges[0].payload.request_id)
    assert.equal(state.exchanges[1].payload.title, 'Changed title')
  })

  for (const scheme of ['light', 'dark']) await scenario({ name: `long content phone/${scheme}`, mobile: true, scheme }, async (page) => {
    // Given / When
    await compose(page, { title: 'LongTitle'.repeat(22), body: 'LongBody'.repeat(125), mandatory: true })
    // Then
    await capture(page, `phone-${scheme}-long-top`)
    await page.getByRole('button', { name: 'Back to edit' }).scrollIntoViewIfNeeded()
    await capture(page, `phone-${scheme}-long-end`)
    await page.setViewportSize({ width: 320, height: 640 })
    await capture(page, `phone-${scheme}-320`)
  })
  console.log(JSON.stringify({ passed: evidence.length, scenarios: evidence, output }))
} finally {
  await server.close()
  await writeFile(join(output, 'receipt.json'), JSON.stringify({ scenarios: evidence, output, cleanup: { browserClosed: true, viteClosed: true, syntheticAPIsOnly: true } }, null, 2))
}
