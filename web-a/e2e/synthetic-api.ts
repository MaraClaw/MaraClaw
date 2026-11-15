import type { BrowserContext } from '@playwright/test'
import { purchaseOrder } from '../tests/capacity-fixtures.ts'
import { pendingTransfer } from '../tests/transfer-review-fixtures.ts'

export type Actor = 'platform_admin' | 'org_admin' | 'member' | 'forced' | 'anonymous'
export const paymentAliases = [
  { path: `/transfer-payments/${pendingTransfer.id}`, heading: 'Transfer payment' },
  { path: `/capacity-payments/${purchaseOrder.id}`, heading: 'Capacity payment' },
] as const

const company = {
  id: '11111111-1111-4111-8111-111111111111', name: 'Synthetic Company', slug: 'synthetic-company',
  is_active: true, sso_enabled: false, sso_domain: null, is_system: false,
  is_default_end_user_org: false, can_disable: true, created_at: null,
  user_count: 1, active_user_count: 1, inactive_user_count: 0, agent_count: 0,
  org_admin_email: 'admin@example.test', phone: '+84912345678', tier: 'pro',
} as const

export function syntheticState(actor: Actor = 'platform_admin', theme: 'light' | 'dark' = 'light') {
  const platform = actor === 'platform_admin' || actor === 'forced'
  const requests: { method: string; path: string; body: unknown }[] = []
  const unexpected: string[] = []
  const companies: (typeof company)[] = []
  return {
    actor,
    theme,
    user: {
      id: '22222222-2222-4222-8222-222222222222', identity_id: '33333333-3333-4333-8333-333333333333',
      email: 'operator@example.test', display_name: 'Operator', avatar_url: null,
      role: actor === 'forced' || actor === 'anonymous' ? 'platform_admin' : actor,
      is_platform_admin: platform, tenant_id: platform ? null : company.id,
      is_active: true, email_verified: true, must_change_password: actor === 'forced',
      created_at: '2026-11-01T00:00:00Z',
    },
    requests,
    unexpected,
    companies,
    createGate: Promise.withResolvers<void>(),
    holdCreate: false,
    createStatus: 200,
  }
}

export async function installSyntheticApi(context: BrowserContext, baseURL: string, state: ReturnType<typeof syntheticState>) {
  await context.addInitScript(({ actor, theme, origin }) => {
    if (location.origin !== origin) return
    if (actor !== 'anonymous') localStorage.setItem('maraclaw-admin-token', 'synthetic-selected-admin-token')
    localStorage.setItem('maraclaw-admin-theme', theme)
  }, { actor: state.actor, theme: state.theme, origin: new URL(baseURL).origin })
  await context.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    if (method === 'GET' && url.origin === 'https://fonts.googleapis.com' && path === '/css2') {
      await route.fulfill({ contentType: 'text/css', body: '/* External fonts omitted for offline browser tests. */' })
      return
    }
    if (url.origin === new URL(baseURL).origin && !/^\/(api|ws|p)(\/|$)/.test(path)) return route.continue()
    state.requests.push({ method, path, body: request.postData() ? request.postDataJSON() : null })
    let body: unknown
    let status = 200
    if (method === 'GET' && path === '/api/auth/me' && state.actor !== 'anonymous') body = state.user
    else if (method === 'GET' && path === '/api/notifications/unread-count') body = { unread_count: 0 }
    else if (method === 'GET' && path === '/api/organization-plans') body = { plans: [] }
    else if (method === 'GET' && path === '/api/admin/companies/page') body = { items: state.companies, next_cursor: null }
    else if (method === 'POST' && path === '/api/admin/companies') {
      if (state.holdCreate) await state.createGate.promise
      status = state.createStatus
      if (status === 200) {
        state.companies = [company]
        body = { company, org_admin_email: company.org_admin_email, must_change_password: true }
      } else body = { detail: status === 409 ? 'Admin email already registered.' : 'Only a platform admin can create a company.' }
    } else if (method === 'GET' && path === `/api/company-admissions/transfers/${pendingTransfer.id}`) body = pendingTransfer
    else if (method === 'GET' && path === `/api/admin/capacity-orders/${purchaseOrder.id}`) body = purchaseOrder
    else if (method === 'GET' && path === '/api/users/admin-audit-logs/page') body = { items: [], next_cursor: null }
    else if (method === 'GET' && path === '/api/company-admissions/transfers/review/page') body = { items: [], next_cursor: null }
    else {
      state.unexpected.push(`${method} ${url.href}`)
      await route.abort('blockedbyclient')
      return
    }
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  })
}
