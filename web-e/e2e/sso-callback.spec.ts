import { expect, test } from '@playwright/test'

import { originalCompany, originalUser, prepareWorkspace } from './session-fencing-fixture'

for (const callback of [
  { query: '?provider=example', error: 'This sign-in link is missing a provider or authorization code.' },
  { query: '?provider=example&code=code&state=wrong', error: 'This sign-in attempt expired. Start again from the login page.' },
]) {
  test(`SSO rejects invalid callback ${callback.query} without dispatch`, async ({ page }) => {
    await page.addInitScript(() => sessionStorage.setItem('maraclaw-sso-state', 'expected'))
    const requests: string[] = []
    page.on('request', (request) => {
      if (new URL(request.url()).pathname.endsWith('/callback')) requests.push(request.method())
    })

    await page.goto(`/sso/callback${callback.query}`)

    await expect(page.getByRole('alert')).toHaveText(callback.error)
    await expect(page.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute('href', '/login')
    expect(requests.filter((method) => method === 'POST')).toEqual([])
  })
}

test('SSO retains the stored provider for organization selection after consuming the transaction', async ({ page }) => {
  await prepareWorkspace(page)
  await page.addInitScript(() => {
    sessionStorage.setItem('maraclaw-sso-state', 'expected')
    sessionStorage.setItem('maraclaw-sso-provider', 'example')
  })
  const bodies: unknown[] = []
  await page.route('**/api/auth/example/callback', async (route) => {
    const body: unknown = route.request().postDataJSON()
    bodies.push(body)
    await route.fulfill({ json: bodies.length === 1
      ? { requires_tenant_selection: true, pending_token: 'pending', tenants: [{ tenant_id: originalCompany.id, tenant_name: originalCompany.name }] }
      : { access_token: 'sso-token', user: originalUser },
    })
  })
  await page.goto('/sso/callback?code=code&state=expected')
  await expect(page.getByRole('heading', { name: 'Choose organization' })).toBeVisible()

  await page.getByRole('button', { name: originalCompany.name }).click()

  await expect(page).toHaveURL('/app/agents')
  expect(bodies).toEqual([{ code: 'code' }, { pending_token: 'pending', tenant_id: originalCompany.id }])
  expect(await page.evaluate(() => sessionStorage.getItem('maraclaw-sso-provider'))).toBeNull()
})

test('SSO displays a current asynchronous callback failure', async ({ page }) => {
  await page.route('**/api/auth/example/callback', (route) => route.fulfill({
    status: 403, json: { detail: 'Company sign-in refused' },
  }))

  await page.goto('/sso/callback?provider=example&code=code')

  await expect(page.getByRole('alert')).toHaveText('Company sign-in refused')
  await expect(page.getByRole('link', { name: 'Back to sign in' })).toBeVisible()
})

test('SSO bind completes once and returns to settings', async ({ page }) => {
  await prepareWorkspace(page)
  await page.addInitScript(() => sessionStorage.setItem('maraclaw-sso-intent', 'bind'))
  const bodies: unknown[] = []
  await page.route('**/api/auth/example/bind', async (route) => {
    bodies.push(route.request().postDataJSON())
    await route.fulfill({ json: originalUser })
  })

  await page.goto('/sso/callback?provider=example&code=bind-code')

  await expect(page).toHaveURL('/app/settings')
  await expect(page.getByLabel('Display name')).toHaveValue(originalUser.display_name)
  expect(bodies).toEqual([{ provider_type: 'example', code: 'bind-code' }])
})
