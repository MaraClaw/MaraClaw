import { expect, test, type Page } from '@playwright/test'

import { member, prepareTransfers, source, target } from './company-transfers-fixture'
import { originalUser, prepareWorkspace, writeFromOtherTab } from './session-fencing-fixture'

const invitationPath = '/transfer#code=synthetic-invite'
const continuationKey = 'maraclaw-sso-continuation'

async function prepareInvitationSso(page: Page, options: {
  readonly tenantless?: boolean
  readonly selection?: boolean
  readonly failure?: boolean
} = {}) {
  await prepareTransfers(page, { anonymous: true, tenantless: options.tenantless })
  const bodies: unknown[] = []
  const providerUrls: string[] = []
  await page.route('**/api/auth/providers', (route) => route.fulfill({ json: [
    { id: 'example', provider_type: 'example', name: 'Synthetic SSO', is_active: true },
  ] }))
  await page.route('**/api/auth/example/authorize?**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    expect(request.method()).toBe('GET')
    expect(request.headers().authorization).toBeUndefined()
    expect([...url.searchParams.keys()].sort()).toEqual(['redirect_uri', 'state'])
    expect(url.searchParams.get('redirect_uri')).toBe(`${url.origin}/sso/callback`)
    expect(url.searchParams.get('state')).toMatch(/^[0-9a-f-]{36}$/)
    const authorizationUrl = `https://sso.example.test/authorize?${url.searchParams}`
    await route.fulfill({ json: { authorization_url: authorizationUrl } })
  })
  await page.route('https://sso.example.test/**', async (route) => {
    providerUrls.push(route.request().url())
    const url = new URL(route.request().url())
    expect(url.href).not.toContain('synthetic-invite')
    expect(url.searchParams.get('redirect_uri')).toMatch(/\/sso\/callback$/)
    await route.fulfill({ status: 302, headers: {
      location: `${url.searchParams.get('redirect_uri')}?code=sso-code&state=${url.searchParams.get('state')}`,
    } })
  })
  await page.route('**/api/member/auth/example/callback', async (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().headers().authorization).toBeUndefined()
    bodies.push(route.request().postDataJSON())
    await route.fulfill(options.failure
      ? { status: 403, json: { detail: 'Synthetic SSO refused' } }
      : { json: options.selection && bodies.length === 1
        ? { requires_tenant_selection: true, pending_token: 'pending-sso', login_identifier: member.email, tenants: [{ tenant_id: source.id, tenant_name: source.name }] }
        : { access_token: 'source-token', user: { ...member, tenant_id: options.tenantless ? null : source.id }, needs_org_confirm: options.tenantless ?? false },
      })
  })
  return { bodies, providerUrls }
}

for (const tenantless of [false, true]) {
  for (const selection of [false, true]) {
    test(`SSO resumes an invitation for ${tenantless ? 'tenantless' : 'existing'} members after ${selection ? 'tenant selection' : 'a direct token'}`, async ({ page }, testInfo) => {
      // Given an anonymous invitation and a synthetic provider, with no external authentication.
      const errors: string[] = []
      page.on('pageerror', (error) => errors.push(error.message))
      const sso = await prepareInvitationSso(page, { tenantless, selection })
      await page.goto(invitationPath)
      await expect(page).toHaveURL('/login')
      // When company sign-in completes, including the optional organization choice.
      await page.getByRole('button', { name: 'Synthetic SSO', exact: true }).click()
      if (selection) {
        await expect(page.getByRole('heading', { name: 'Choose organization' })).toBeVisible()
        await page.getByRole('button', { name: source.name, exact: true }).click()
      }
      // Then the invitation is restored only to the local form and is consumed from storage/URL.
      await expect(page).toHaveURL(tenantless ? '/join' : '/transfer')
      await expect(page.getByLabel('Invitation code', { exact: true })).toHaveValue('synthetic-invite')
      await expect(page.getByText(target.name, { exact: true })).toHaveCount(0)
      expect(sso.bodies).toEqual(selection
        ? [{ code: 'sso-code' }, { pending_token: 'pending-sso', tenant_id: source.id }]
        : [{ code: 'sso-code' }])
      expect(sso.providerUrls).toHaveLength(1)
      expect(await page.evaluate(() => Object.keys(sessionStorage).filter((key) => key.startsWith('maraclaw-sso-')))).toEqual([])
      const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }))
      expect(stored).not.toContain('synthetic-invite')
      expect(errors).toEqual([])
      await page.screenshot({ path: testInfo.outputPath('resumed-invitation.png'), fullPage: true })
    })
  }
}

test('SSO clears invitation continuation when the callback fails', async ({ page }) => {
  // Given an invitation waiting on a provider that refuses the sign-in.
  await prepareInvitationSso(page, { failure: true })
  await page.goto(invitationPath)
  // When SSO fails.
  await page.getByRole('button', { name: 'Synthetic SSO', exact: true }).click()
  // Then retrying cannot reuse the invitation transaction.
  await expect(page.getByRole('alert')).toHaveText('Synthetic SSO refused')
  expect(await page.evaluate(() => Object.keys(sessionStorage).filter((key) => key.startsWith('maraclaw-sso-')))).toEqual([])
})

test('SSO rejects a stored external continuation instead of navigating it', async ({ page }) => {
  // Given a tampered transaction in this tab, with a valid synthetic callback.
  await prepareInvitationSso(page, { tenantless: true })
  await page.addInitScript(({ continuationKey }) => {
    sessionStorage.setItem('maraclaw-sso-state', 'expected')
    sessionStorage.setItem('maraclaw-sso-provider', 'example')
    sessionStorage.setItem(continuationKey, JSON.stringify({ state: 'expected', provider: 'example', path: 'https://evil.example/transfer#code=synthetic-invite' }))
  }, { continuationKey })
  // When SSO returns.
  await page.goto('/sso/callback?code=sso-code&state=expected')
  // Then the normal local join destination is used, without an invitation.
  await expect(page).toHaveURL('/join')
  await expect(page.getByLabel('Invitation code', { exact: true })).toHaveValue('')
  expect(await page.evaluate((key) => sessionStorage.getItem(key), continuationKey)).toBeNull()
})

test('SSO clears invitation continuation on state rejection without calling the provider callback', async ({ page }) => {
  // Given a valid stored invitation transaction.
  const sso = await prepareInvitationSso(page)
  await page.addInitScript(({ continuationKey, invitationPath }) => {
    sessionStorage.setItem('maraclaw-sso-state', 'expected')
    sessionStorage.setItem('maraclaw-sso-provider', 'example')
    sessionStorage.setItem(continuationKey, JSON.stringify({ state: 'expected', provider: 'example', path: invitationPath }))
  }, { continuationKey, invitationPath })
  // When a different SSO transaction returns.
  await page.goto('/sso/callback?code=sso-code&state=wrong')
  // Then it fails closed and discards the invitation.
  await expect(page.getByRole('alert')).toContainText('expired')
  expect(sso.bodies).toEqual([])
  expect(await page.evaluate((key) => sessionStorage.getItem(key), continuationKey)).toBeNull()
})

test('SSO clears invitation continuation when authorization cannot start', async ({ page }) => {
  // Given
  await prepareInvitationSso(page)
  await page.route('**/api/auth/example/authorize?**', (route) => route.fulfill({ status: 503, json: { detail: 'Synthetic authorization unavailable' } }))
  await page.goto(invitationPath)
  // When
  const response = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/authorize'))
  await page.getByRole('button', { name: 'Synthetic SSO', exact: true }).click()
  await response
  // Then
  await expect.poll(() => page.evaluate(() => Object.keys(sessionStorage).filter((key) => key.startsWith('maraclaw-sso-')))).toEqual([])
  await expect(page).toHaveURL('/login')
})

test('SSO bind ignores a stored invitation and returns to settings', async ({ page }) => {
  // Given
  await prepareWorkspace(page)
  await page.addInitScript(({ continuationKey, invitationPath }) => {
    sessionStorage.setItem('maraclaw-sso-state', 'bind-state')
    sessionStorage.setItem('maraclaw-sso-provider', 'example')
    sessionStorage.setItem('maraclaw-sso-intent', 'bind')
    sessionStorage.setItem(continuationKey, JSON.stringify({ state: 'bind-state', provider: 'example', path: invitationPath }))
  }, { continuationKey, invitationPath })
  const bodies: unknown[] = []
  await page.route('**/api/member/auth/example/bind', async (route) => {
    bodies.push(route.request().postDataJSON())
    await route.fulfill({ json: originalUser })
  })
  // When
  await page.goto('/sso/callback?code=bind-code&state=bind-state')
  // Then
  await expect(page).toHaveURL('/app/settings')
  expect(bodies).toEqual([{ provider_type: 'example', code: 'bind-code' }])
  expect(await page.evaluate((key) => sessionStorage.getItem(key), continuationKey)).toBeNull()
})

test('a late SSO callback cannot replace a newer owner or resume its invitation', async ({ page }) => {
  // Given
  await prepareInvitationSso(page)
  const entered = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const completed = Promise.withResolvers<void>()
  await page.route('**/api/member/auth/example/callback', async (route) => {
    entered.resolve()
    await release.promise
    await route.fulfill({ json: { access_token: 'source-token', user: member } })
    completed.resolve()
  })
  await page.goto(invitationPath)
  await page.getByRole('button', { name: 'Synthetic SSO', exact: true }).click()
  await entered.promise
  try {
    // When another tab replaces the session before the SSO response arrives.
    const refreshed = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/member/auth/me')
    await writeFromOtherTab(page, 'replacement-token')
    await refreshed
    release.resolve()
    await completed.promise
    // Then the stale completion cannot install its token or navigate to the invitation.
    await expect.poll(() => page.evaluate((key) => sessionStorage.getItem(key), continuationKey)).toBeNull()
    expect(await page.evaluate(() => localStorage.getItem('maraclaw-enduser-token'))).toBe('replacement-token')
    await expect(page).toHaveURL(/\/sso\/callback\?/)
  } finally {
    release.resolve()
  }
})
