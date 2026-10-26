import { expect, test } from '@playwright/test'
import { controlPage, controlPath, credentialPath, otherCredential, prepareCapture, savedCredential } from './control-capture-fixture'
import { replacementToken, replacementUser, writeFromOtherTab } from './session-fencing-fixture'

test('use-only members explicitly create a credential without enumerating the vault', async ({ page }) => {
  // Given
  const fixture = await prepareCapture(page, 'use')
  await page.goto(controlPage)
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByLabel('Cookie domain on unlock')).toHaveValue('vendor.example.test')
  await expect(page.getByLabel('Save cookies on release')).toHaveValue('create')
  // When
  await page.getByRole('button', { name: 'Release control', exact: true }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
  expect(fixture.requests.find((request) => request.endpoint === 'unlock')?.body).toEqual({
    session_id: 'control-session', owner_token: 'capture-owner', export_cookies: true,
    platform_hint: 'vendor.example.test', capture: { mode: 'create' },
  })
  expect(fixture.credentialReads()).toBe(0)
  await expect(page.getByRole('option', { name: /Team account|Other account/ })).toHaveCount(0)
  expect(fixture.errors).toEqual([])
})

test('replacing one of two same-platform credentials uses its selected ID and observed revision', async ({ page }) => {
  // Given
  const fixture = await prepareCapture(page)
  await page.goto(controlPage)
  await expect(page.getByRole('option', { name: /Other account/ })).toHaveCount(1)
  await page.getByLabel('Save cookies on release').selectOption(otherCredential.id)
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByLabel('Cookie domain on unlock')).toHaveValue('vendor.example.test')
  // When
  await page.getByRole('button', { name: 'Release control', exact: true }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
  expect(fixture.requests.find((request) => request.endpoint === 'unlock')?.body).toEqual({
    session_id: 'control-session', owner_token: 'capture-owner', export_cookies: true,
    platform_hint: 'vendor.example.test', capture: { mode: 'replace', credential_id: otherCredential.id, expected_revision: 3 },
  })
  expect(fixture.errors).toEqual([])
})

test('a background vault refresh does not silently rebase a selected revision', async ({ page }) => {
  // Given
  const fixture = await prepareCapture(page)
  await page.clock.install()
  await page.goto(controlPage)
  await page.getByLabel('Save cookies on release').selectOption(savedCredential.id)
  let refreshed = false
  await page.route(`**${credentialPath}`, (route) => {
    refreshed = true
    return route.fulfill({ json: [{ ...savedCredential, revision: 8 }] })
  })
  await page.clock.fastForward(61_000)
  await page.evaluate(() => {
    window.dispatchEvent(new Event('offline'))
    window.dispatchEvent(new Event('online'))
  })
  await expect.poll(() => refreshed).toBe(true)
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByLabel('Cookie domain on unlock')).toHaveValue('vendor.example.test')
  // When
  await page.getByRole('button', { name: 'Release control', exact: true }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
  expect(fixture.requests.find((request) => request.endpoint === 'unlock')?.body).toMatchObject({
    capture: { mode: 'replace', credential_id: savedCredential.id, expected_revision: 7 },
  })
})

for (const failure of ['changed', 'deleted'] as const) {
  test(`a ${failure} capture target releases local control without reporting lease loss or retrying`, async ({ page }) => {
    // Given
    const fixture = await prepareCapture(page)
    await page.clock.install()
    await page.goto(controlPage)
    await page.getByLabel('Save cookies on release').selectOption(savedCredential.id)
    await page.getByRole('button', { name: 'Take control', exact: true }).click()
    await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
    let unlocks = 0
    await page.route(`**${controlPath}unlock`, async (route) => {
      unlocks++
      await route.fulfill({ status: failure === 'changed' ? 409 : 404, json: {
        detail: failure === 'changed' ? { code: 'credential_revision_conflict' } : 'Credential not found',
      } })
    })
    // When
    await page.getByRole('button', { name: 'Release control', exact: true }).click()
    // Then
    await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
    await expect(page.getByRole('alert')).toContainText('Control released. Cookies were not saved')
    await expect(page.getByAltText('Live AgentBay session')).toHaveCount(0)
    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toHaveCount(0)
    await expect(page.getByLabel('Cookie domain on unlock')).toBeEmpty()
    await expect(page.getByLabel('Save cookies on release')).toHaveValue('create')
    const count = fixture.requests.length
    await page.clock.runFor(10_000)
    expect(fixture.requests).toHaveLength(count)
    expect(unlocks).toBe(1)
    expect(fixture.errors).toEqual([])
  })
}

test('release without saving sends no capture or domain', async ({ page }) => {
  // Given
  const fixture = await prepareCapture(page)
  await page.goto(controlPage)
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByLabel('Cookie domain on unlock')).toHaveValue('vendor.example.test')
  await page.getByLabel('Save cookies on release').selectOption('skip')
  // When
  await page.getByRole('button', { name: 'Release control', exact: true }).click()
  // Then
  await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
  expect(fixture.requests.find((request) => request.endpoint === 'unlock')?.body).toEqual({
    session_id: 'control-session', owner_token: 'capture-owner', export_cookies: false,
  })
})

test('an unrelated 409 does not masquerade as lease loss', async ({ page }) => {
  // Given
  await prepareCapture(page)
  await page.goto(controlPage)
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
  await page.route(`**${controlPath}press_keys`, (route) => route.fulfill({ status: 409, json: {
    detail: { code: 'some_other_conflict', message: 'The remote action could not complete' },
  } }))
  // When
  await page.getByRole('button', { name: 'Enter', exact: true }).click()
  // Then
  await expect(page.locator('[data-sonner-toast][data-type="error"]')).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Release control', exact: true })).toBeEnabled()
  await expect(page.locator('[data-sonner-toast][data-type="error"]')).not.toContainText('lock was lost')
})

for (const outcome of ['success', 'conflict'] as const) {
  test(`a retired session's delayed capture ${outcome} cannot notify or change the replacement workspace`, async ({ page }) => {
    // Given
    await prepareCapture(page)
    const pending = Promise.withResolvers<void>()
    const received = Promise.withResolvers<void>()
    await page.route(`**${controlPath}unlock`, async (route) => {
      received.resolve()
      await pending.promise
      await route.fulfill(outcome === 'success'
        ? { json: { status: 'unlocked', cookies_exported: true, cookie_count: 2 } }
        : { status: 409, json: { detail: { code: 'credential_revision_conflict' } } })
    })
    try {
      await page.goto(controlPage)
      await page.getByRole('button', { name: 'Take control', exact: true }).click()
      await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
      await page.getByRole('button', { name: 'Release control', exact: true }).click()
      await received.promise
      await writeFromOtherTab(page, replacementToken)
      await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
      await expect(page.getByText(`Signed in as ${replacementUser.display_name}.`, { exact: false })).toBeVisible()
      const completed = page.waitForEvent('requestfinished', {
        predicate: (request) => request.url().endsWith(`${controlPath}unlock`),
      })
      // When
      pending.resolve()
      await completed
      // Then
      await expect(page).toHaveURL('/app/settings')
      await expect(page.getByRole('alert')).toHaveCount(0)
      await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    } finally {
      pending.resolve()
    }
  })
}

test('release fencing suppresses polling and a concurrent stale lease-loss response', async ({ page }) => {
  // Given
  const fixture = await prepareCapture(page)
  await page.clock.install()
  await page.goto(controlPage)
  await page.getByLabel('Save cookies on release').selectOption(savedCredential.id)
  await page.getByRole('button', { name: 'Take control', exact: true }).click()
  await expect(page.getByAltText('Live AgentBay session')).toBeVisible()
  const screenshot = Promise.withResolvers<void>()
  const screenshotSeen = Promise.withResolvers<void>()
  const unlock = Promise.withResolvers<void>()
  const unlockSeen = Promise.withResolvers<void>()
  let screenshots = 0
  await page.route(`**${controlPath}screenshot`, async (route) => {
    screenshots++
    screenshotSeen.resolve()
    await screenshot.promise
    await route.fulfill({ status: 409, json: { detail: { code: 'take_control_lost' } } })
  })
  await page.route(`**${controlPath}unlock`, async (route) => {
    unlockSeen.resolve()
    await unlock.promise
    await route.fulfill({ status: 409, json: { detail: { code: 'credential_revision_conflict' } } })
  })
  try {
    await page.clock.runFor(2500)
    await screenshotSeen.promise
    await page.getByRole('button', { name: 'Release control', exact: true }).click()
    await unlockSeen.promise
    const completed = page.waitForEvent('requestfinished', {
      predicate: (request) => request.url().endsWith(`${controlPath}screenshot`),
    })
    // When
    screenshot.resolve()
    await completed
    await page.clock.runFor(10_000)
    // Then
    await expect(page.locator('[data-sonner-toast][data-type="error"]')).toHaveCount(0)
    await expect(page.getByLabel('Save cookies on release')).toBeDisabled()
    expect(screenshots).toBe(1)
    unlock.resolve()
    await expect(page.getByRole('alert')).toContainText('selected credential changed')
    await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeEnabled()
    expect(fixture.errors).toEqual([])
  } finally {
    screenshot.resolve()
    unlock.resolve()
  }
})
