import { expect, test, type Page } from '@playwright/test'

import { originalCompany, originalUser, prepareWorkspace } from './session-fencing-fixture'

const stateKey = 'maraclaw-sso-state'
const providerKey = 'maraclaw-sso-provider'
const intentKey = 'maraclaw-sso-intent'

async function seedSso(page: Page, stored: Readonly<Record<string, string>>) {
  await page.addInitScript((entries) => {
    for (const [key, value] of Object.entries(entries)) sessionStorage.setItem(key, value)
  }, stored)
}

function watchEngineCallbacks(page: Page) {
  const requests: string[] = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && (path.endsWith('/callback') || path.endsWith('/bind'))) requests.push(path)
  })
  return requests
}

const expired = 'This sign-in attempt expired. Start again from the login page.'

for (const callback of [
  { name: 'a missing authorization code', stored: { [stateKey]: 'expected' }, query: '?provider=example', error: 'This sign-in link is missing a provider or authorization code.' },
  { name: 'a state that differs from the stored one', stored: { [stateKey]: 'expected' }, query: '?provider=example&code=code&state=wrong', error: expired },
  { name: 'a URL without a state', stored: { [stateKey]: 'expected' }, query: '?provider=example&code=code', error: expired },
  { name: 'no stored state at all', stored: { [providerKey]: 'example' }, query: '?provider=example&code=code&state=expected', error: expired },
  { name: 'no stored state for a bind', stored: { [providerKey]: 'example', [intentKey]: 'bind' }, query: '?provider=example&code=code&state=expected', error: expired },
]) {
  test(`SSO rejects ${callback.name} without dispatch`, async ({ page }) => {
    await seedSso(page, callback.stored)
    const requests = watchEngineCallbacks(page)

    await page.goto(`/sso/callback${callback.query}`)

    await expect(page.getByRole('alert')).toHaveText(callback.error)
    await expect(page.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute('href', '/login')
    expect(requests).toEqual([])
  })
}

test('SSO retains the stored provider for organization selection after consuming the transaction', async ({ page }) => {
  await prepareWorkspace(page)
  await seedSso(page, { [stateKey]: 'expected', [providerKey]: 'example' })
  const bodies: unknown[] = []
  await page.route('**/api/member/auth/example/callback', async (route) => {
    const body: unknown = route.request().postDataJSON()
    bodies.push(body)
    await route.fulfill({ json: bodies.length === 1
      ? { requires_tenant_selection: true, pending_token: 'pending', login_identifier: originalUser.email, tenants: [{ tenant_id: originalCompany.id, tenant_name: originalCompany.name, tenant_slug: originalCompany.slug }] }
      : { access_token: 'sso-token', user: originalUser },
    })
  })
  await page.goto('/sso/callback?code=code&state=expected')
  await expect(page.getByRole('heading', { name: 'Choose organization' })).toBeVisible()

  await page.getByRole('button', { name: originalCompany.name }).click()

  await expect(page).toHaveURL('/app/agents')
  expect(bodies).toEqual([{ code: 'code', state: 'expected' }, { pending_token: 'pending', tenant_id: originalCompany.id }])
  expect(await page.evaluate(() => sessionStorage.getItem('maraclaw-sso-provider'))).toBeNull()
})

for (const failure of [
  { detail: 'Company sign-in refused', message: 'Company sign-in refused' },
  { detail: { code: 'member_audience_rejected', message: 'Member audience required' }, message: 'Member audience required' },
]) {
  test(`SSO displays a current asynchronous callback failure: ${failure.message}`, async ({ page }) => {
    await seedSso(page, { [stateKey]: 'expected' })
    await page.route('**/api/member/auth/example/callback', (route) => route.fulfill({
      status: 403, json: { detail: failure.detail },
    }))

    await page.goto('/sso/callback?provider=example&code=code&state=expected')

    await expect(page.getByRole('alert')).toHaveText(failure.message)
    await expect(page.getByRole('link', { name: 'Back to sign in' })).toBeVisible()
  })
}

test('SSO bind completes once with the stored state and returns to settings', async ({ page }) => {
  await prepareWorkspace(page)
  await seedSso(page, { [stateKey]: 'bind-state', [intentKey]: 'bind' })
  const bodies: unknown[] = []
  await page.route('**/api/member/auth/example/bind', async (route) => {
    bodies.push(route.request().postDataJSON())
    await route.fulfill({ json: originalUser })
  })

  await page.goto('/sso/callback?provider=example&code=bind-code&state=bind-state')

  await expect(page).toHaveURL('/app/settings')
  await expect(page.getByLabel('Display name')).toHaveValue(originalUser.display_name)
  expect(bodies).toEqual([{ provider_type: 'example', code: 'bind-code', state: 'bind-state' }])
})

test('Connect in settings links the account with the server state and the bind intent', async ({ page }) => {
  // Given a signed-in member, a provider, and an authorize endpoint that mints its own state.
  await prepareWorkspace(page)
  const bodies: unknown[] = []
  await page.route('**/api/auth/providers', (route) => route.fulfill({ json: [
    { id: 'example', provider_type: 'example', name: 'Synthetic SSO', is_active: true },
  ] }))
  await page.route('**/api/auth/example/authorize?**', async (route) => {
    const url = new URL(route.request().url())
    expect(route.request().headers().authorization).toBeUndefined()
    expect([...url.searchParams.keys()].sort()).toEqual(['intent', 'redirect_uri'])
    expect(url.searchParams.get('intent')).toBe('bind')
    expect(url.searchParams.get('redirect_uri')).toBe(`${url.origin}/sso/callback`)
    await route.fulfill({ json: {
      authorization_url: `https://sso.example.test/authorize?redirect_uri=${encodeURIComponent(url.searchParams.get('redirect_uri') ?? '')}&state=server-bind-state`,
      state: 'server-bind-state',
    } })
  })
  await page.route('https://sso.example.test/**', async (route) => {
    const url = new URL(route.request().url())
    await route.fulfill({ status: 302, headers: {
      location: `${url.searchParams.get('redirect_uri')}?code=bind-code&state=${url.searchParams.get('state')}`,
    } })
  })
  await page.route('**/api/member/auth/example/bind', async (route) => {
    bodies.push(route.request().postDataJSON())
    await route.fulfill({ json: originalUser })
  })
  await page.goto('/app/settings')

  // When the member connects the provider and the provider returns.
  await page.getByRole('button', { name: 'Connect' }).click()

  // Then the bind request carries the server-issued state kept in sessionStorage, and the transaction is consumed.
  await expect(page).toHaveURL('/app/settings')
  await expect.poll(() => bodies).toEqual([{ provider_type: 'example', code: 'bind-code', state: 'server-bind-state' }])
  expect(await page.evaluate(() => Object.keys(sessionStorage).filter((key) => key.startsWith('maraclaw-sso-')))).toEqual([])
})
