import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { confirmCancellation, emptyStatus, metric, sendReceipt } from './broadcast-browser-harness.mjs'
import { runRetiredReceiptCases } from './broadcast-email-retirement-cases.mjs'
import { runBroadcastEmailFocusCases } from './broadcast-email-focus-cases.mjs'

export async function runBroadcastEmailCases(harness) {
  const { scenario, capture } = harness
  await scenario({ name: 'receipt polls queued and claimed-only work, then stops at terminal counts' }, async (page, state) => {
    // Given
    await page.clock.install()
    state.emailGate = Promise.withResolvers()
    const progress = await sendReceipt(page)
    await expect(progress.getByRole('status').filter({ hasText: 'Loading email progress...' })).toBeVisible()
    await capture(page, 'email-loading')
    state.emailGate.resolve(); state.emailGate = null
    await metric(progress, 'Queued', 5)
    state.email = { ...state.email, queued: 0, claimed: 5 }
    // When
    await page.clock.fastForward(5_000)
    // Then
    await metric(progress, 'Claimed', 5)
    await expect(progress.getByRole('button', { name: 'Cancel queued email', exact: true })).toBeDisabled()
    state.email = { ...state.email, claimed: 0, smtp_accepted: 4, failed: 1 }
    await page.clock.fastForward(5_000)
    await metric(progress, 'SMTP accepted', 4); await metric(progress, 'Failed', 1)
    const requests = state.emailRequests.length
    await page.clock.fastForward(60_000)
    assert.equal(state.emailRequests.length, requests)
    assert.equal(state.exchanges.length, 1)
    await capture(page, 'email-terminal')
  })

  for (const width of [390, 1440, 1920]) for (const scheme of ['light', 'dark']) {
    await scenario({ name: `${width} ${scheme} separately confirmed partial cancellation reconciles worker race`, mobile: width === 390, width, scheme }, async (page, state) => {
      // Given
      await page.clock.install()
      state.receipt = { ...state.receipt, emails_sent: 12 }
      state.email = { ...state.email, queued: 5, claimed: 2, smtp_accepted: 3, failed: 1, cancelled: 1 }
      const progress = await sendReceipt(page)
      for (const [label, value] of [['Queued', 5], ['Claimed', 2], ['SMTP accepted', 3], ['Failed', 1], ['Cancelled', 1]]) await metric(progress, label, value)
      const prefix = `email-${width}-${scheme}`
      await progress.getByRole('heading', { name: 'Email progress', exact: true }).scrollIntoViewIfNeeded()
      await capture(page, `${prefix}-top`)
      const cancel = progress.getByRole('button', { name: 'Cancel queued email', exact: true })
      await cancel.focus(); await capture(page, `${prefix}-end-focus`)
      await cancel.press('Enter')
      const heading = progress.getByRole('heading', { name: 'Cancel queued email?', exact: true })
      await expect(heading).toBeFocused(); await heading.press('Enter')
      assert.equal(state.cancelRequests.length, 0)
      await heading.press('Tab'); await expect(progress.getByRole('button', { name: 'Keep queued email' })).toBeFocused()
      await page.keyboard.press('Escape'); await expect(cancel).toBeFocused()
      await cancel.click(); await expect(heading).toBeFocused()
      await progress.getByRole('button', { name: 'Keep queued email' }).click(); await expect(cancel).toBeFocused()
      assert.equal(state.cancelRequests.length, 0)
      await cancel.click(); await expect(heading).toBeFocused()
      const confirm = progress.getByRole('button', { name: 'Confirm cancel queued email', exact: true })
      await confirm.scrollIntoViewIfNeeded()
      await capture(page, `${prefix}-confirmation`)
      state.cancelGate = Promise.withResolvers()
      state.cancelResult = { ...state.email, queued: 0, claimed: 5, cancelled: 3, cancelled_now: 2 }
      await confirm.focus()
      // When
      await confirm.evaluate((button) => { button.click(); button.click() })
      // Then
      await expect.poll(() => state.cancelRequests.length).toBe(1)
      await expect(progress.getByRole('button', { name: 'Cancelling queued email...' })).toBeDisabled()
      await expect(progress.getByRole('button', { name: 'Keep queued email' })).toBeDisabled()
      await expect(progress.getByRole('status').filter({ hasText: 'Cancelling unclaimed email and refreshing progress...' })).toBeFocused()
      const beforeReconcile = state.emailRequests.length
      await page.clock.fastForward(30_000)
      assert.equal(state.cancelRequests.length, 1)
      assert.equal(state.emailRequests.length, beforeReconcile)
      await capture(page, `${prefix}-pending`)
      state.emailGate = Promise.withResolvers()
      state.email = { ...state.cancelResult, claimed: 0, smtp_accepted: 8 }
      state.cancelGate.resolve()
      await expect.poll(() => state.emailRequests.length).toBe(beforeReconcile + 1)
      await metric(progress, 'Cancelled', 3); await metric(progress, 'Claimed', 5)
      state.emailGate.resolve()
      await metric(progress, 'SMTP accepted', 8); await metric(progress, 'Claimed', 0)
      await expect(progress.getByRole('status').filter({ hasText: 'Cancelled 2 queued email targets in this request.' })).toBeFocused()
      await expect(cancel).toBeDisabled()
      await capture(page, `${prefix}-reconciled`)
      const requests = state.emailRequests.length
      await page.clock.fastForward(60_000)
      assert.equal(state.emailRequests.length, requests)
      assert.equal(state.cancelRequests.length, 1)
      assert.equal(state.exchanges.length, 1)
    })
  }

  for (const cancelled of [0, 3]) await scenario({ name: `cancellation honestly reports ${cancelled === 0 ? 'no remaining' : 'all queued'} targets` }, async (page, state) => {
    // Given
    state.email = { ...state.email, queued: 3, claimed: 2 }
    const progress = await sendReceipt(page)
    await metric(progress, 'Queued', 3)
    state.cancelResult = { ...state.email, queued: 0, claimed: 5 - cancelled, cancelled, cancelled_now: cancelled }
    state.email = { ...state.cancelResult, claimed: 0, smtp_accepted: 5 - cancelled }
    // When
    await confirmCancellation(progress)
    // Then
    await metric(progress, 'Cancelled', cancelled)
    await metric(progress, 'SMTP accepted', 5 - cancelled)
    await expect(progress.getByRole('status').filter({ hasText: `Cancelled ${cancelled} queued email targets in this request.` })).toBeFocused()
    await expect(progress.getByRole('button', { name: 'Cancel queued email', exact: true })).toBeDisabled()
    assert.equal(state.cancelRequests.length, 1)
    await capture(page, `email-cancel-${cancelled}`)
  })

  await scenario({ name: 'unavailable status is generic, has no aggregate detail and stops polling' }, async (page, state) => {
    // Given
    await page.clock.install()
    state.email = emptyStatus
    // When
    const progress = await sendReceipt(page)
    // Then
    await expect(progress.getByText('Email progress is unavailable.', { exact: true })).toBeVisible()
    await expect(progress.locator('dl')).toHaveCount(0)
    await expect(progress.getByRole('button', { name: 'Cancel queued email', exact: true })).toBeDisabled()
    const requests = state.emailRequests.length
    await page.clock.fastForward(60_000)
    assert.equal(state.emailRequests.length, requests)
    await capture(page, 'email-unavailable')
  })

  for (const sendEmail of [false, true]) await scenario({ name: `${sendEmail ? 'email with no recipients' : 'in-app only'} receipt makes no email API requests` }, async (page, state) => {
    // Given
    await page.clock.install()
    state.receipt = { ...state.receipt, emails_sent: 0 }
    // When
    const progress = await sendReceipt(page, { title: 'No email targets', body: '', send_email: sendEmail })
    // Then
    await expect(progress.getByText('Email progress is unavailable. No email targets were queued.')).toBeVisible()
    await expect(progress.getByRole('button')).toHaveCount(0)
    await page.clock.fastForward(60_000)
    assert.equal(state.emailRequests.length, 0); assert.equal(state.cancelRequests.length, 0)
    assert.equal(state.exchanges.length, 1)
    await capture(page, `email-zero-${sendEmail}`)
  })

  await scenario({ name: 'status errors do not retry automatically, manual refresh recovers and stale counts disable cancellation' }, async (page, state) => {
    // Given
    await page.clock.install()
    state.emailHttpStatus = 503
    const progress = await sendReceipt(page)
    await expect(progress.getByRole('alert')).toContainText('Could not refresh email progress.')
    await capture(page, 'email-status-error')
    const requests = state.emailRequests.length
    await page.clock.fastForward(60_000)
    assert.equal(state.emailRequests.length, requests)
    state.emailHttpStatus = 200
    // When
    await progress.getByRole('button', { name: 'Refresh email progress', exact: true }).click()
    // Then
    await metric(progress, 'Queued', 5)
    state.emailHttpStatus = 503
    await page.clock.fastForward(5_000)
    await expect(progress.getByRole('alert')).toBeVisible()
    await metric(progress, 'Queued', 5)
    await expect(progress.getByRole('button', { name: 'Cancel queued email', exact: true })).toBeDisabled()
    await page.clock.fastForward(60_000)
    assert.equal(state.emailRequests.length, requests + 2)
    assert.equal(state.exchanges.length, 1)
    await capture(page, 'email-stale-status')
  })

  await scenario({ name: 'ambiguous cancel failure refreshes partial counts and only explicit reconfirmation retries' }, async (page, state) => {
    // Given
    await page.clock.install()
    const progress = await sendReceipt(page)
    await metric(progress, 'Queued', 5)
    state.cancelHttpStatus = 503
    state.email = { ...state.email, queued: 1, claimed: 3, cancelled: 1 }
    // When
    await confirmCancellation(progress)
    // Then
    await expect(progress.getByRole('alert')).toBeFocused()
    await metric(progress, 'Queued', 1); await metric(progress, 'Cancelled', 1)
    await page.clock.fastForward(30_000)
    assert.equal(state.cancelRequests.length, 1)
    assert.equal(state.exchanges.length, 1)
    await capture(page, 'email-cancel-error-reconciled')
    state.cancelHttpStatus = 200
    state.cancelResult = { ...state.email, queued: 0, cancelled: 2, cancelled_now: 1 }
    state.email = { ...state.cancelResult, claimed: 0, smtp_accepted: 3 }
    await confirmCancellation(progress)
    await metric(progress, 'Cancelled', 2)
    assert.equal(state.cancelRequests.length, 2)
  })

  await scenario({ name: 'cancellation becoming unavailable replaces old aggregates without leaking why' }, async (page, state) => {
    // Given
    const progress = await sendReceipt(page)
    await metric(progress, 'Queued', 5)
    state.cancelResult = { ...emptyStatus, cancelled_now: 0 }; state.email = emptyStatus
    // When
    await confirmCancellation(progress)
    // Then
    await expect(progress.locator('dl')).toHaveCount(0)
    await expect(progress.getByRole('button', { name: 'Cancel queued email', exact: true })).toBeDisabled()
    await expect(progress.getByRole('alert')).toHaveCount(0)
    await capture(page, 'email-cancel-unavailable')
  })

  await scenario({ name: 'confirmation cannot cancel targets already claimed during polling' }, async (page, state) => {
    // Given
    await page.clock.install()
    const progress = await sendReceipt(page)
    await metric(progress, 'Queued', 5)
    await progress.getByRole('button', { name: 'Cancel queued email', exact: true }).click()
    state.email = { ...state.email, queued: 0, claimed: 5 }
    // When
    await page.clock.fastForward(5_000)
    // Then
    await metric(progress, 'Claimed', 5)
    await expect(progress.getByRole('button', { name: 'Confirm cancel queued email', exact: true })).toBeDisabled()
    assert.equal(state.cancelRequests.length, 0)
    await progress.getByRole('button', { name: 'Keep queued email' }).click()
  })
  await runBroadcastEmailFocusCases(harness)
  await runRetiredReceiptCases(harness)
}
