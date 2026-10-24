import { expect, type Page } from '@playwright/test'
import { z } from 'zod'

import type { TenantChoice, UserOut } from '../src/lib/types/auth'
import type { AgentOut } from '../src/lib/workspace-api'

export const tokenKey = 'maraclaw-enduser-token'
export const originalToken = 'fencing-original-session'
export const replacementToken = 'fencing-replacement-session'
export const originalCompany = {
  id: 'd39e5319-8475-4d73-b34c-7d5a71cb5011', name: 'Fencing Original Company', slug: 'fencing-original',
} as const
export const replacementCompany = {
  id: 'e9c9d722-3f86-4ae1-b829-435079266026', name: 'Fencing Replacement Company', slug: 'fencing-replacement',
} as const
export const originalUser: UserOut = {
  id: '47f8d9a9-17eb-4b58-a0e3-fbc6a00d18db',
  email: 'original@fencing.test', display_name: 'Original fencing member', role: 'member',
  tenant_id: originalCompany.id, email_verified: true, must_change_password: false, is_platform_admin: false,
}
export const replacementUser: UserOut = {
  ...originalUser, id: '49926b42-c074-4aa1-9aa1-54f0adbf70b9',
  email: 'replacement@fencing.test', display_name: 'Replacement fencing member', tenant_id: replacementCompany.id,
}
export const originalAgent: AgentOut = {
  id: '5458ec51-a954-4306-8626-68e16921d95f', name: 'Original company agent',
  role_description: 'Belongs only to the original company', status: 'stopped', creator_id: originalUser.id,
  access_level: 'manage',
}
export const replacementAgent: AgentOut = {
  ...originalAgent, id: '4f14671e-0517-4bb1-b0b5-8772db8ad2e9', name: 'Replacement company agent',
  role_description: 'Belongs only to the replacement company', creator_id: replacementUser.id,
}
export const delayedAgent: AgentOut = {
  ...originalAgent, id: '466ed379-3187-43b6-ac44-e7b21fbbec32', name: 'Delayed original-company creation',
}

export async function prepareWorkspace(page: Page, nextUser = replacementUser, switchToken = replacementToken) {
  // The fake changes its server-side identity on switch, including same-token reissuance.
  let switched = false
  let tickets = 0
  const delayedSession = {
    id: '31bccbda-b487-4da0-bb4f-7ab6800a9433', agent_id: delayedAgent.id, user_id: originalUser.id,
    title: 'Shared conversation', source_channel: 'shared', is_primary: true,
    created_at: '2030-01-01T00:00:00Z', message_count: 0,
  }
  await page.addInitScript(({ tokenKey, originalToken }) => {
    localStorage.setItem(tokenKey, originalToken)
  }, { tokenKey, originalToken })
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const replacing = request.headers().authorization === `Bearer ${replacementToken}` || (switched && switchToken === originalToken)
    const company = replacing ? replacementCompany : originalCompany
    switch (path) {
      case '/api/auth/chat-ticket': {
        expect(request.method()).toBe('POST')
        expect(request.headers().authorization).toBe(`Bearer ${replacing ? replacementToken : originalToken}`)
        const body = z.object({ agent_id: z.string(), session_id: z.string().optional() }).parse(request.postDataJSON())
        await route.fulfill({ json: { ticket: `opaque-workspace-${++tickets}`, session_id: body.session_id ?? 'resolved-session' } })
        return
      }
      case '/api/auth/me':
        await route.fulfill({ json: replacing ? nextUser : originalUser })
        return
      case '/api/auth/my-tenants': {
        const choices: TenantChoice[] = [originalCompany, replacementCompany].map((tenant) => ({
          tenant_id: tenant.id, tenant_name: tenant.name, tenant_slug: tenant.slug, logo_url: null,
        }))
        await route.fulfill({ json: choices })
        return
      }
      case '/api/auth/switch-tenant':
        expect(request.method()).toBe('POST')
        expect(request.postDataJSON()).toEqual({ tenant_id: replacementCompany.id })
        switched = true
        await route.fulfill({ json: { access_token: switchToken } })
        return
      case '/api/tenants/me':
        await route.fulfill({ json: company })
        return
      case '/api/agents/':
        expect(request.method(), 'Creation must be handled by the deferred response gate').toBe('GET')
        await route.fulfill({ json: [replacing ? replacementAgent : originalAgent] })
        return
      case '/api/onboarding/status':
        await route.fulfill({ json: { exists: true, status: 'completed', current_step: 'completed' } })
        return
      case '/api/notifications/unread-count':
        await route.fulfill({ json: { unread_count: 0 } })
        return
      case '/api/agents/templates':
      case '/api/auth/providers':
        await route.fulfill({ json: [] })
        return
      case `/api/agents/${delayedAgent.id}/sessions/${delayedSession.id}/messages/page`:
        expect(new URL(request.url()).searchParams.get('limit')).toBe('50')
        await route.fulfill({ json: { items: [], next_cursor: null, has_more: false } })
        return
      case `/api/agents/${originalAgent.id}/openclaw-update`:
      case `/api/agents/${replacementAgent.id}/openclaw-update`:
      case `/api/agents/${delayedAgent.id}/openclaw-update`:
        expect(request.method()).toBe('GET')
        expect(request.headers().authorization).toBe(`Bearer ${replacing ? switchToken : originalToken}`)
        await route.fulfill({ json: { notice: null, run: null, required: false, can_upgrade: false } })
        return
      case `/api/agents/${delayedAgent.id}/sessions`:
        await route.fulfill({ json: request.method() === 'POST' ? delayedSession : [delayedSession] })
        return
      case `/api/agents/${delayedAgent.id}`:
        await route.fulfill({ json: delayedAgent })
        return
      case `/api/agents/${delayedAgent.id}/metrics`:
        await route.fulfill({ json: { status: delayedAgent.status, tasks: { total: 0, done: 0, pending: 0 } } })
        return
      default:
        expect(path, 'Unexpected fake API endpoint').toBe('a supported session-fencing route')
    }
  })
  await page.routeWebSocket('**/ws/**', (socket) => socket.close({ code: 4003, reason: 'Fixture denies chat' }))
}

export async function writeFromOtherTab(page: Page, token: string | null) {
  const peer = await page.context().newPage()
  try {
    await peer.route('**/session-fencing-peer', (route) => route.fulfill({
      contentType: 'text/html', body: '<!doctype html><title>Storage event peer</title>',
    }))
    await peer.goto('/session-fencing-peer')
    await peer.evaluate(({ tokenKey, token }) => {
      if (token === null) localStorage.removeItem(tokenKey)
      else localStorage.setItem(tokenKey, token)
    }, { tokenKey, token })
  } finally {
    await peer.close()
  }
}

export async function switchFromSettings(page: Page) {
  await page.getByRole('link', { name: 'Settings', exact: true }).first().click()
  const row = page.locator('main div').filter({
    has: page.getByText(replacementCompany.name, { exact: true }),
  }).filter({ has: page.getByRole('button', { name: 'Switch', exact: true }) }).last()
  await row.getByRole('button', { name: 'Switch', exact: true }).click()
}

export async function deferCreation(page: Page, outcome: 'success' | 'error') {
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  await page.route('**/api/agents/', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    expect(route.request().headers().authorization).toBe(`Bearer ${originalToken}`)
    expect(route.request().postDataJSON()).toMatchObject({ name: delayedAgent.name })
    requested.resolve()
    await release.promise
    switch (outcome) {
      case 'success':
        await route.fulfill({ json: delayedAgent })
        return
      case 'error':
        await route.fulfill({ status: 403, json: { detail: 'Original-company creation refused' } })
        return
      default:
        outcome satisfies never
    }
  })
  return { requested: requested.promise, release: () => release.resolve() }
}
