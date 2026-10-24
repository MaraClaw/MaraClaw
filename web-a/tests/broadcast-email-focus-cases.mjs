import assert from 'node:assert/strict'
import { expect } from '@playwright/test'
import { metric, sendReceipt } from './broadcast-browser-harness.mjs'

export async function runBroadcastEmailFocusCases(harness) {
  const { scenario, capture } = harness
  await scenario({ name: 'background polling retains refresh keyboard focus without duplicate requests' }, async (page, state) => {
    // Given
    await page.clock.install()
    const progress = await sendReceipt(page)
    await metric(progress, 'Queued', 5)
    const refresh = progress.getByRole('button', { name: 'Refresh email progress', exact: true })
    await refresh.focus()
    const requests = state.emailRequests.length
    state.emailGate = Promise.withResolvers()
    // When
    await page.clock.fastForward(5_000)
    // Then
    await expect.poll(() => state.emailRequests.length).toBe(requests + 1)
    const refreshing = progress.getByRole('button', { name: 'Refreshing progress...', exact: true })
    await expect(refreshing).toBeFocused()
    await refreshing.press('Enter')
    assert.equal(state.emailRequests.length, requests + 1)
    await capture(page, 'email-poll-focus')
    state.emailGate.resolve()
    await expect(refresh).toBeFocused()
  })

  for (const dismiss of ['keep', 'escape']) await scenario({ name: `${dismiss} restores progress heading focus when polling removes cancellable work` }, async (page, state) => {
    // Given
    await page.clock.install()
    const progress = await sendReceipt(page)
    await metric(progress, 'Queued', 5)
    await progress.getByRole('button', { name: 'Cancel queued email', exact: true }).click()
    state.email = { ...state.email, queued: 0, claimed: 5 }
    await page.clock.fastForward(5_000)
    await metric(progress, 'Claimed', 5)
    await expect(progress.getByRole('button', { name: 'Confirm cancel queued email', exact: true })).toBeDisabled()
    // When
    if (dismiss === 'keep') await progress.getByRole('button', { name: 'Keep queued email' }).click()
    else await page.keyboard.press('Escape')
    // Then
    await expect(progress.getByRole('heading', { name: 'Email progress', exact: true })).toBeFocused()
    assert.equal(state.cancelRequests.length, 0)
    await capture(page, `email-dismiss-${dismiss}-focus`)
  })
}
