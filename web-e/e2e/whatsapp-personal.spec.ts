import { expect, test } from '@playwright/test'

import { channelApiRoot, channelsPath, prepareChannels } from './channels-fixture'
import { replacementToken, replacementUser, writeFromOtherTab } from './session-fencing-fixture'
import { personalAttempt, personalGeneration, personalRoot, preparePersonal, syntheticQr, unlinked } from './whatsapp-personal-fixture'

test('native clock keeps polling from unchanged pending to QR and cleans up on refresh and unmount', async ({ page }, testInfo) => {
  // Given: native browser timers, with one pending revision repeated before a new QR.
  await prepareChannels(page)
  const errors: string[] = []
  const reads: { readonly state: string; readonly revision: number }[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  const expiresAt = new Date(Date.now() + 120000).toISOString()
  const pending = { ...syntheticQr, state: 'pending', qr: null, expires_at: expiresAt } as const
  const ready = { ...syntheticQr, revision: 2, expires_at: expiresAt, qr: { value: 'synthetic-native-clock-pairing', expires_at: expiresAt } }
  await page.route(`**${channelApiRoot}/whatsapp-channel`, (route) => {
    expect(route.request().method()).toBe('GET')
    return route.fulfill({ json: { mode: 'personal_qr' } })
  })
  await page.route(`**${personalRoot}**`, (route) => {
    expect(route.request().method()).toBe('GET')
    expect(new URL(route.request().url()).pathname).toBe(personalRoot)
    const status = reads.length < 2 ? pending : ready
    reads.push({ state: status.state, revision: status.revision })
    return route.fulfill({ headers: { 'Cache-Control': 'no-store, private' }, json: status })
  })
  try {
    // When: the mounted card follows the real two-second polling lifecycle.
    await page.goto(channelsPath)
    const card = page.getByRole('article', { name: 'WhatsApp', exact: true })
    const qr = card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })
    // Then: a newer revision becomes scannable and polling continues after it.
    await expect(qr).toBeVisible({ timeout: 10000 })
    await expect.poll(() => reads.length, { timeout: 5000 }).toBeGreaterThanOrEqual(4)
    expect(reads.slice(0, 4)).toEqual([{ state: 'pending', revision: 1 }, { state: 'pending', revision: 1 }, { state: 'qr', revision: 2 }, { state: 'qr', revision: 2 }])
    const refreshed = page.waitForResponse((response) => new URL(response.url()).pathname === personalRoot)
    await card.getByRole('button', { name: 'Refresh status', exact: true }).click()
    await refreshed
    await expect(qr).toBeVisible()
    await card.getByRole('radio', { name: 'Cloud API', exact: true }).check()
    await expect(page.getByRole('region', { name: 'Personal QR connection', exact: true })).toHaveCount(0)
    const readsAtUnmount = reads.length
    await page.evaluate(() => new Promise<void>((resolve) => setTimeout(resolve, 2100)))
    expect(reads).toHaveLength(readsAtUnmount)
    expect(errors).toEqual([])
  } finally {
    await testInfo.attach('native-clock-lifecycle', { body: JSON.stringify({ reads, errors }), contentType: 'application/json' })
  }
})

test('creator starts QR pairing with the nullable generation and sees connected self-chat state', async ({ page }) => {
  // Given: an unconfigured personal account and existing Cloud form.
  const fixture = await preparePersonal(page)
  await page.goto(channelsPath)
  const card = page.getByRole('article', { name: 'WhatsApp', exact: true })
  for (const label of ['Access token', 'Phone number ID', 'Verify token', 'App secret', 'API version']) await expect(card.getByLabel(label, { exact: true })).toBeVisible()
  await card.getByRole('radio', { name: 'Personal QR', exact: true }).check()
  await expect(card.getByRole('button', { name: 'Connect personal account', exact: true })).toBeVisible()
  // When: the creator explicitly connects and the provider reports a scan.
  await card.getByRole('button', { name: 'Connect personal account', exact: true }).click()
  await expect(card).toContainText('Preparing a pairing code')
  fixture.setStatus({ ...syntheticQr, revision: 2 })
  await page.clock.runFor(2000)
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toBeVisible()
  fixture.setStatus({ ...syntheticQr, state: 'connected', revision: 3, qr: null, account_hint: '••42' })
  await page.clock.runFor(2000)
  // Then: connected status replaces the QR, without adding storage or URL secrets.
  await expect(card).toContainText('Personal account connected.')
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toHaveCount(0)
  expect(fixture.bodies[0]).toEqual({ expected_generation: null })
  expect(await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, url: location.href }))).not.toContain(syntheticQr.qr?.value)
})

test('expired QR disappears locally and relink uses the current generation', async ({ page }) => {
  // Given: an expiring personal QR.
  const fixture = await preparePersonal(page, { status: syntheticQr, mode: 'personal_qr' })
  await page.goto(channelsPath)
  const card = page.getByRole('article', { name: 'WhatsApp', exact: true })
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toBeVisible()
  // When: its deadline elapses without a provider update.
  await page.clock.runFor(60001)
  // Then: the QR is gone and recovery carries the fenced generation.
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toHaveCount(0)
  await expect(card).toContainText('Pairing code expired.')
  await card.getByRole('button', { name: 'Relink account', exact: true }).click()
  expect(fixture.bodies[0]).toEqual({ expected_generation: personalGeneration })
  await expect(card).toContainText('previous connection is still stopping')
  await fixture.reap()
  await page.clock.runFor(2000)
  await expect(card).toContainText('Preparing a pairing code')
  fixture.setStatus({ ...fixture.getStatus(), revision: fixture.getStatus().revision + 1, state: 'qr', qr: { value: 'synthetic-relinked-after-expiry', expires_at: '2030-01-01T00:02:00Z' } })
  await page.clock.runFor(2000)
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toBeVisible()
})

test('refresh keeps the current unexpired QR scannable without starting another attempt', async ({ page }) => {
  // Given: the server holds one current QR revision.
  const fixture = await preparePersonal(page, { status: syntheticQr, mode: 'personal_qr' })
  await page.goto(channelsPath)
  const card = page.getByRole('article', { name: 'WhatsApp', exact: true })
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toBeVisible()
  // When: the creator refreshes that unchanged status.
  await card.getByRole('button', { name: 'Refresh status', exact: true }).click()
  // Then: the real QR image remains available with no pairing mutation.
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toBeVisible()
  expect(fixture.operations.filter((operation) => operation === 'GET status')).toHaveLength(2)
  expect(fixture.bodies).toEqual([])
})

test('disconnect clears QR and reports stopping rather than claiming a completed disconnect', async ({ page }) => {
  // Given: a current personal pairing attempt.
  const fixture = await preparePersonal(page, { status: syntheticQr, mode: 'personal_qr' })
  await page.goto(channelsPath)
  const card = page.getByRole('article', { name: 'WhatsApp', exact: true })
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toBeVisible()
  // When: the creator disconnects.
  await card.getByRole('button', { name: 'Disconnect personal account', exact: true }).click()
  // Then: QR removal and disconnecting status reflect the server, not optimistic socket death.
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toHaveCount(0)
  await expect(card).toContainText('previous connection is still stopping')
  expect(fixture.operations).toContain('DELETE status')
  expect(fixture.getStatus().attempt_id).toBe(personalAttempt)
  await fixture.reap()
  await page.clock.runFor(2000)
  await expect(card).toContainText('No personal account connected')
  await card.getByRole('button', { name: 'Refresh status', exact: true }).click()
  await expect(card).toContainText('No personal account connected')
  await expect(card.getByRole('alert')).toHaveCount(0)
})

test('live-child relink polls same-attempt teardown, new pending attempt and new QR', async ({ page }) => {
  // Given: a live connected child still owns its original attempt.
  const fixture = await preparePersonal(page, { status: { ...syntheticQr, state: 'connected', qr: null, revision: 4 }, mode: 'personal_qr' })
  await page.goto(channelsPath)
  const card = page.getByRole('article', { name: 'WhatsApp', exact: true })
  await expect(card).toContainText('Personal account connected.')
  // When: relink revokes it before reap creates a new attempt.
  await card.getByRole('button', { name: 'Relink account', exact: true }).click()
  await expect(card).toContainText('previous connection is still stopping')
  expect(fixture.getStatus().attempt_id).toBe(personalAttempt)
  await fixture.reap()
  await page.clock.runFor(2000)
  await expect(card).toContainText('Preparing a pairing code')
  expect(fixture.getStatus().attempt_id).not.toBe(personalAttempt)
  fixture.setStatus({ ...fixture.getStatus(), revision: fixture.getStatus().revision + 1, state: 'qr', qr: syntheticQr.qr })
  await page.clock.runFor(2000)
  // Then: the new code appears without another mutation or manual refresh.
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toBeVisible()
  expect(fixture.operations.filter((operation) => operation === 'POST /relink')).toHaveLength(1)
})

test('stale generation conflict exposes refresh and never automatically repeats pairing', async ({ page }) => {
  // Given: a concurrent change makes the expected generation stale.
  const fixture = await preparePersonal(page, { mode: 'personal_qr' })
  let starts = 0
  await page.route(`**${personalRoot}/start`, (route) => { starts += 1; return route.fulfill({ status: 409, json: { detail: 'stale generation' } }) })
  await page.goto(channelsPath)
  const card = page.getByRole('article', { name: 'WhatsApp', exact: true })
  // When: the creator starts pairing.
  await card.getByRole('button', { name: 'Connect personal account', exact: true }).click()
  // Then: safe recovery needs an explicit status refresh, not a retry with null generation.
  await expect(card.getByRole('alert')).toContainText('Connection changed elsewhere')
  await expect(card.getByRole('button', { name: 'Connect personal account', exact: true })).toHaveCount(0)
  fixture.setStatus({ ...unlinked, generation: personalGeneration, attempt_id: personalAttempt, revision: 4 })
  await card.getByRole('button', { name: 'Refresh status', exact: true }).click()
  await expect(card.getByRole('button', { name: 'Relink account', exact: true })).toBeVisible()
  expect(starts).toBe(1)
})

test('noncreator makes no private reads and sees no method or pairing controls', async ({ page }) => {
  // Given: a manager who is not the creator.
  const fixture = await preparePersonal(page, { creator: false })
  // When: they view the channels tab.
  await page.goto(channelsPath)
  // Then: the catalog remains visible without private setup or reads.
  const card = page.getByRole('article', { name: 'WhatsApp', exact: true })
  await expect(card).toBeVisible()
  await expect(card.getByRole('radio')).toHaveCount(0)
  await expect(card.getByRole('button')).toHaveCount(0)
  expect(fixture.operations).toEqual([])
})

test('switching to Cloud removes QR and keeps all five canonical Cloud save fields', async ({ page }) => {
  // Given: a current personal QR.
  const fixture = await preparePersonal(page, { status: syntheticQr, mode: 'personal_qr' })
  await page.goto(channelsPath)
  const card = page.getByRole('article', { name: 'WhatsApp', exact: true })
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toBeVisible()
  // When: the creator changes method and saves Cloud credentials.
  await card.getByRole('radio', { name: 'Cloud API', exact: true }).check()
  await card.getByLabel('Access token', { exact: true }).fill('synthetic-access')
  await card.getByLabel('Phone number ID', { exact: true }).fill('123456')
  await card.getByLabel('Verify token', { exact: true }).fill('synthetic-verify')
  await card.getByLabel('App secret', { exact: true }).fill('synthetic-app')
  await card.getByLabel('API version', { exact: true }).fill('v99.0')
  await card.getByRole('button', { name: 'Save', exact: true }).click()
  // Then: no pairing secret remains in the DOM and the original Cloud payload is preserved.
  await expect(card.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toHaveCount(0)
  await expect.poll(() => fixture.bodies.length).toBe(1)
  expect(fixture.bodies[0]).toEqual({ access_token: 'synthetic-access', phone_number_id: '123456', verify_token: 'synthetic-verify', app_secret: 'synthetic-app', api_version: 'v99.0' })
})

test('a held pairing completion cannot reappear after cross-tab owner replacement', async ({ page }) => {
  // Given: a start response held across session retirement.
  await preparePersonal(page, { mode: 'personal_qr' })
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route(`**${personalRoot}/start`, async (route) => { requested.resolve(); await release.promise; await route.fulfill({ status: 202, json: syntheticQr }) })
  try {
    await page.goto(channelsPath)
    await page.getByRole('button', { name: 'Connect personal account', exact: true }).click()
    await requested.promise
    // When: another synthetic session replaces the owner before completion.
    await writeFromOtherTab(page, replacementToken)
    await expect(page.getByText(replacementUser.email, { exact: true }).first()).toBeVisible()
    release.resolve()
    // Then: no stale pairing controls or secret can appear for the replacement session.
    await expect(page.getByRole('img', { name: 'WhatsApp pairing QR', exact: true })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Connect personal account', exact: true })).toHaveCount(0)
  } finally { release.resolve() }
})
