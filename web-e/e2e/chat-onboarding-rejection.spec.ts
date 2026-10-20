import { expect, test } from '@playwright/test'
import { accept, chatPath, prepareChat, submitted } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'

for (const reason of ['no_company_model', 'quota_exceeded', 'enqueue_failed', 'agent_expired'] as const) {
  test(`onboarding ${reason} without a submission ID shows actionable feedback`, async ({ page }, testInfo) => {
    // Given
    const chat = await prepareChat(page)
    chat.setOnboarded(false)
    await page.goto(chatPath())
    const socket = await chat.socket()
    await expect.poll(() => socket.sent).toEqual([JSON.stringify({ kind: 'onboarding_trigger' })])
    const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
    await composer.fill('An unfinished draft')

    // When
    socket.route.send(JSON.stringify({ type: 'turn_rejected', reason, content: 'Onboarding blocked.' }))

    // Then
    await expect(page.getByText(/Onboarding could not start\..*Onboarding blocked\..*Ask an admin/)).toBeVisible()
    await expect(page.getByText('Malformed chat event', { exact: true })).toHaveCount(0)
    await expect(page.locator('article')).toHaveCount(0)
    await expect(composer).toHaveValue('An unfinished draft')
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled()
    await page.screenshot({ path: testInfo.outputPath(`${reason}.png`), fullPage: true })
  })
}

for (const accepted of [false, true]) {
  test(`uncorrelated onboarding rejection does not recover an ordinary ${accepted ? 'accepted' : 'pending'} turn`, async ({ page }) => {
    // Given
    const chat = await prepareChat(page)
    chat.setOnboarded(false)
    await page.goto(chatPath())
    const socket = await chat.socket()
    await expect.poll(() => socket.sent.length).toBe(1)
    const composer = page.getByRole('textbox', { name: `Message ${originalAgent.name}` })
    await composer.fill('An ordinary submitted question')
    await composer.press('Enter')
    await expect.poll(() => socket.sent.length).toBe(2)
    const envelope = submitted(socket, 1)
    if (accepted) accept(socket, envelope.client_submission_id)
    await composer.fill('A separate draft')

    // When
    socket.route.send(JSON.stringify({ type: 'turn_rejected', reason: 'enqueue_failed', content: 'Onboarding blocked.' }))

    // Then
    await expect(page.getByText(/Onboarding could not start\..*Onboarding blocked\./)).toBeVisible()
    await expect(page.locator('article')).toHaveText(['An ordinary submitted question'])
    await expect(page.getByRole('button', { name: 'Retry this message', exact: true })).toHaveCount(0)
    await expect(page.locator('[aria-busy]')).toHaveAttribute('aria-busy', 'true')
    await expect(composer).toHaveValue('A separate draft')
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled()
    socket.route.close({ code: 1006 })
    const reconnected = await chat.socket(1)
    if (accepted) expect(reconnected.sent).toEqual([])
    else await expect.poll(() => reconnected.sent).toEqual([socket.sent[1]])
  })
}
