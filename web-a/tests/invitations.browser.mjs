import assert from 'node:assert/strict'
import { createInvitationsHarness, expect, invitation } from './invitations-browser-harness.mjs'

const harness = await createInvitationsHarness()
const fixtures = [
  invitation,
  { ...invitation, id: 'used', code: 'SYNTHETIC-USED', invited_email: 'used@example.test', used_count: 1, status: 'used', redeemed_at: '2026-10-24T13:00:00Z' },
  { ...invitation, id: 'expired', code: 'SYNTHETIC-EXPIRED', invited_email: 'expired@example.test', status: 'expired' },
  { ...invitation, id: 'revoked', code: 'SYNTHETIC-REVOKED', invited_email: 'revoked@example.test', status: 'revoked', is_active: false },
  { ...invitation, id: 'legacy', code: 'SYNTHETIC-LEGACY-'.repeat(6), invited_email: null, expires_at: null },
  { ...invitation, id: 'zero', code: 'SYNTHETIC-ZERO', invited_email: 'zero@example.test', max_uses: 0, status: 'used' },
]
let completed = false
try {
  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) {
    const suffix = `${width}-${scheme}`
    await harness.scenario({ name: `loading and empty ${suffix}`, width, scheme, listGate: Promise.withResolvers() }, async (page, state) => {
      await expect(page.getByRole('heading', { name: 'Invitations', exact: true })).toBeVisible()
      await expect(page.getByText('Loading invitations…')).toBeVisible()
      await harness.capture(page, `loading-${suffix}`)
      state.listGate.resolve()
      await expect(page.getByText(/No invitations yet/)).toBeVisible()
      await harness.capture(page, `empty-${suffix}`)
    })
    await harness.scenario({ name: `history error and recovery ${suffix}`, width, scheme, listStatus: 503 }, async (page, state) => {
      await expect(page.getByText('Could not load invitation history.')).toBeVisible()
      await harness.capture(page, `history-error-${suffix}`)
      state.listStatus = 200
      await page.getByRole('button', { name: 'Retry history' }).click()
      await expect(page.getByText(/No invitations yet/)).toBeVisible()
    })
    await harness.scenario({ name: `populated and actions ${suffix}`, width, scheme, items: fixtures }, async (page, state) => {
      await expect(page.getByRole('table', { name: 'Invitation history' })).toBeVisible()
      await expect(page.getByText('Used', { exact: true })).toHaveCount(2)
      await expect(page.getByText('Expired', { exact: true })).toBeVisible()
      await expect(page.getByText('Revoked', { exact: true })).toBeVisible()
      await expect(page.getByRole('row').filter({ hasText: 'zero@example.test' }).getByText('0 / 0', { exact: true })).toBeVisible()
      await harness.capture(page, `populated-${suffix}`)
      await page.locator('main').evaluate((element) => { element.scrollTop = element.scrollHeight })
      await harness.capture(page, `history-end-${suffix}`)
      const email = page.getByRole('textbox', { name: 'Recipient emails' })
      const ttl = page.getByRole('spinbutton', { name: 'Valid for (days)' })
      const send = page.getByRole('button', { name: 'Send invitation emails' })
      await expect(ttl).toHaveValue('14')
      await expect(ttl).toHaveAttribute('min', '1')
      await expect(ttl).toHaveAttribute('max', '14')
      await expect(ttl).toHaveAttribute('step', '1')
      await expect(email).toHaveAttribute('required', '')
      await expect(email).toHaveAttribute('type', 'text')
      await expect(page.getByText(/separate emails with semicolons/)).toBeVisible()
      await send.click()
      await expect(email).toHaveAttribute('aria-invalid', 'true')
      await expect(email).toBeFocused()
      assert.equal(state.exchanges.length, 0)
      await harness.capture(page, `missing-recipient-${suffix}`)
      for (const invalid of ['invalid', 'alice@example.com;invalid', ';alice@example.com', 'alice@example.com;', 'alice@example.com;;bob@example.com', 'alice@example.com; ;bob@example.com', 'alice@example.com,bob@example.com']) {
        await email.fill(invalid)
        await send.click()
        await expect(email).toHaveAttribute('aria-invalid', 'true')
        await expect(email).toBeFocused()
        await expect(page.getByRole('alert')).toBeVisible()
        assert.equal(state.exchanges.length, 0)
      }
      await harness.capture(page, `invalid-recipients-${suffix}`)
      await email.fill(' Alice@Example.COM ; BOB@Example.COM; alice@example.com ')
      for (const invalid of ['0', '1.5', '', '15']) {
        await ttl.fill(invalid)
        await send.click()
        await expect(ttl).toHaveAttribute('aria-invalid', 'true')
        await expect(ttl).toBeFocused()
        assert.equal(state.exchanges.length, 0)
      }
      await harness.capture(page, `invalid-ttl-${suffix}`)
      await ttl.fill('14')
      state.sendGate = Promise.withResolvers()
      await send.click()
      await expect(page.getByRole('button', { name: 'Sending invitations…' })).toBeDisabled()
      await expect(email).toBeDisabled()
      await expect(ttl).toBeDisabled()
      await harness.capture(page, `sending-${suffix}`)
      state.listGate = Promise.withResolvers()
      state.sendGate.resolve()
      await expect(page.getByText('2 invitations created. Email delivery queued.', { exact: true })).toBeFocused()
      await expect(page.getByText('Refreshing history…')).toBeVisible()
      await expect(page.getByText('recipient@example.test', { exact: true })).toBeVisible()
      assert.deepEqual(state.exchanges, [{ method: 'POST', body: { emails: ['alice@example.com', 'bob@example.com'], ttl_hours: 336 } }])
      await harness.capture(page, `sent-refresh-preserved-${suffix}`)
      state.listGate.resolve()
      state.listGate = null
      await expect(page.getByText('alice@example.com', { exact: true })).toBeVisible()
      await expect(page.getByText('bob@example.com', { exact: true })).toBeVisible()
      await expect(email).toHaveValue('')
      await expect(ttl).toHaveValue('14')
      state.sendGate = null
      state.sendStatus = 400
      await email.fill('other@example.test')
      await send.click()
      await expect(page.getByRole('link', { name: 'Open Settings' })).toHaveAttribute('href', '/settings')
      await harness.capture(page, `smtp-error-${suffix}`)
      const revoke = page.getByRole('button', { name: 'Revoke invitation for recipient@example.test', exact: true })
      await revoke.click()
      await expect(page.getByText('Revoke invitation?', { exact: true })).toBeFocused()
      await harness.capture(page, `revoke-confirm-${suffix}`)
      await page.keyboard.press('Escape')
      await expect(revoke).toBeFocused()
      await expect(page.getByRole('button', { name: 'Confirm revoke' })).toHaveCount(0)
      await revoke.click()
      await page.getByRole('button', { name: 'Cancel', exact: true }).click()
      await expect(revoke).toBeFocused()
      assert.equal(state.exchanges.filter((exchange) => exchange.method === 'DELETE').length, 0)
      await revoke.click()
      state.revokeStatus = 503
      await page.getByRole('button', { name: 'Confirm revoke' }).click()
      await expect(page.getByText(/Could not revoke the invitation/)).toBeVisible()
      await harness.capture(page, `revoke-error-${suffix}`)
      state.revokeStatus = 204
      await page.getByRole('button', { name: 'Confirm revoke' }).click()
      const row = page.getByRole('row').filter({ hasText: 'recipient@example.test' })
      await expect(row.getByText('Revoked', { exact: true })).toBeVisible()
      await expect(row.getByRole('button', { name: /Revoke/ })).toHaveCount(0)
      await harness.capture(page, `revoked-${suffix}`)
      assert.equal(state.exchanges.filter((exchange) => exchange.method === 'DELETE').length, 2)
      assert.equal(new URL(page.url()).search, '')
    })
    await harness.scenario({ name: `shell after ${suffix}`, width, scheme, path: 'broadcast' }, async (page) => {
      await expect(page.getByRole('heading', { name: 'Organization broadcast' })).toBeVisible()
      await expect(page.getByRole('link', { name: 'Invitations', exact: true })).toBeVisible()
      await harness.capture(page, `after-shell-${suffix}`)
    })
  }
  await harness.scenario({ name: 'history pagination isolates pages', items: Array.from({ length: 21 }, (_, index) => ({ ...invitation, id: `page-${index}`, code: `SYNTHETIC-${index}`, invited_email: `person-${index}@example.test` })) }, async (page, state) => {
    await expect(page.getByText('person-0@example.test', { exact: true })).toBeVisible()
    state.listGate = Promise.withResolvers()
    await page.getByRole('button', { name: 'Next', exact: true }).click()
    await expect(page.getByText('Loading invitations…')).toBeVisible()
    await expect(page.getByText('Page 2', { exact: true })).toBeVisible()
    await harness.capture(page, 'page-two-loading-1440-light')
    state.listGate.resolve()
    state.listGate = null
    await expect(page.getByText('person-20@example.test', { exact: true })).toBeVisible()
    await expect(page.getByText('person-0@example.test', { exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Next', exact: true })).toBeDisabled()
    await harness.capture(page, 'page-two-1440-light')
    await page.getByRole('button', { name: 'Previous', exact: true }).click()
    await expect(page.getByText('person-0@example.test', { exact: true })).toBeVisible()
    assert.ok(state.lists.includes('?page=2&page_size=20'))
  })
  await harness.scenario({ name: 'one day converts to 24 wire hours' }, async (page, state) => {
    await page.getByRole('textbox', { name: 'Recipient emails' }).fill('one-day@example.test')
    await page.getByRole('spinbutton', { name: 'Valid for (days)' }).fill('1')
    await page.getByRole('button', { name: 'Send invitation emails' }).click()
    await expect(page.getByText('1 invitation created. Email delivery queued.', { exact: true })).toBeVisible()
    assert.deepEqual(state.exchanges[0], { method: 'POST', body: { emails: ['one-day@example.test'], ttl_hours: 24 } })
    await harness.capture(page, 'one-day-1440-light')
  })
  await harness.scenario({ name: 'literal semicolon batch uses the default lifetime' }, async (page, state) => {
    await page.getByRole('textbox', { name: 'Recipient emails' }).fill('alice@example.com; bob@example.com')
    await page.getByRole('button', { name: 'Send invitation emails' }).click()
    await expect(page.getByText('2 invitations created. Email delivery queued.', { exact: true })).toBeVisible()
    assert.deepEqual(state.exchanges, [{ method: 'POST', body: { emails: ['alice@example.com', 'bob@example.com'], ttl_hours: 336 } }])
    await harness.capture(page, 'literal-batch-1440-light')
  })
  for (const invited of [0, 1]) {
    await harness.scenario({ name: `batch feedback uses server count ${invited}`, invited }, async (page, state) => {
      await page.getByRole('textbox', { name: 'Recipient emails' }).fill('alice@example.com; bob@example.com')
      await page.getByRole('button', { name: 'Send invitation emails' }).click()
      await expect(page.getByText(invited === 1 ? '1 invitation created. Email delivery queued.' : 'No invitations were created. Check history before trying again.', { exact: true })).toBeFocused()
      assert.equal(state.exchanges.length, 1)
      await harness.capture(page, `server-count-${invited}-1440-light`)
    })
  }
  await harness.scenario({ name: 'additional admin receives actionable SMTP escalation', user: { is_genesis: false } }, async (page, state) => {
    state.sendStatus = 400
    await page.getByRole('textbox', { name: 'Recipient emails' }).fill('other@example.test')
    await page.getByRole('button', { name: 'Send invitation emails' }).click()
    await expect(page.getByText(/Ask your organization’s genesis administrator/)).toBeVisible()
    await expect(page.getByRole('link', { name: 'Open Settings' })).toHaveCount(0)
    await harness.capture(page, 'smtp-additional-admin-1440-light')
  })
  await harness.scenario({ name: 'tenantless organization admin cannot access invitations', path: 'invitations', user: { tenant_id: null } }, async (page, state) => {
    await expect(page.getByRole('link', { name: 'Invitations', exact: true })).toHaveCount(0)
    await expect(page).not.toHaveURL(/\/invitations$/)
    assert.equal(state.lists.length, 0)
  })
  completed = true
} finally { await harness.close(completed) }
