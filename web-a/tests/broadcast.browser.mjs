import assert from 'node:assert/strict'
import { join } from 'node:path'
import { expect } from '@playwright/test'
import { assertReducedPress } from './company-deletion-browser-evidence.mjs'
import { admin, compose, createBroadcastHarness, details, flush } from './broadcast-browser-harness.mjs'
import { runBroadcastEmailCases } from './broadcast-email-browser-cases.mjs'

const harness = await createBroadcastHarness()
const { base, output, scenario, capture } = harness
let completed = false

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
    await page.getByLabel('Email', { exact: true }).fill('replacement@example.test')
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
  await runBroadcastEmailCases(harness)
  completed = true
} finally {
  await harness.close(completed)
}
