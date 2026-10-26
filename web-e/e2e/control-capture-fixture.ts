import { expect, type Page } from '@playwright/test'
import { originalAgent, originalToken, prepareWorkspace } from './session-fencing-fixture'

export const controlPath = `/api/agents/${originalAgent.id}/control/`
export const controlPage = `/app/agents/${originalAgent.id}/control`
export const credentialPath = `/api/agents/${originalAgent.id}/credentials/`
export const preview = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI4MDAiIGhlaWdodD0iNDAwIj48cmVjdCB3aWR0aD0iODAwIiBoZWlnaHQ9IjQwMCIgZmlsbD0iI2Q3ZTNlOCIvPjwvc3ZnPg=='
export const savedCredential = {
  id: '09bc8102-a06b-4cb5-b034-3e32ea78f67a', agent_id: originalAgent.id, revision: 7,
  credential_type: 'website', platform: 'vendor.example.test', display_name: 'Team account', status: 'active',
  has_cookies: true, cookies_updated_at: null, last_login_at: null, last_injected_at: null,
  created_at: null, updated_at: null,
} as const
export const otherCredential = {
  ...savedCredential, id: '8abe3a44-7589-4f6a-adab-f5d54b65bba1', display_name: 'Other account', revision: 3,
} as const

export async function prepareCapture(page: Page, access: 'use' | 'manage' = 'manage') {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: { ...originalAgent, access_level: access } }))
  await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: {} }))
  await page.route(`**/api/agents/${originalAgent.id}/sessions?**`, (route) => route.fulfill({ json: [
    { id: 'control-session', title: 'Control thread', source_channel: 'shared' },
  ] }))
  let credentialReads = 0
  await page.route(`**${credentialPath}`, (route) => {
    expect(access, 'Use-only members must never enumerate the vault').toBe('manage')
    expect(route.request().method()).toBe('GET')
    credentialReads++
    return route.fulfill({ json: [savedCredential, otherCredential] })
  })
  const requests: { endpoint: string; body: unknown }[] = []
  await page.route(`**${controlPath}*`, async (route) => {
    const endpoint = new URL(route.request().url()).pathname.slice(controlPath.length)
    expect(route.request().method()).toBe('POST')
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    requests.push({ endpoint, body: route.request().postDataJSON() })
    await route.fulfill({ json: {
      status: endpoint === 'lock' ? 'locked' : endpoint === 'unlock' ? 'unlocked' : 'ok',
      owner_token: 'capture-owner', screenshot: preview, url: 'https://vendor.example.test/login',
      cookies_exported: endpoint === 'unlock', cookie_count: 2,
    } })
  })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  return { requests, errors, credentialReads: () => credentialReads }
}
