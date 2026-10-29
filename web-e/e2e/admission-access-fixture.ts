import { expect, type Page } from '@playwright/test'

import type { UserOut } from '../src/lib/types/auth'
import type { AdmissionDetail } from '../src/lib/types/company-admissions'
import type { OnboardingStatus } from '../src/lib/onboarding-api'

export const tokenKey = 'maraclaw-enduser-token'
export const source = { id: 'bdd07e40-83a6-48f3-9049-5ffae4774f75', name: 'KABERs', slug: 'kabers' }
export const target = { id: 'c919ad90-90be-4a1d-b699-1d02138b7f45', name: 'Example Company', slug: 'example' }
export const member: UserOut = {
  id: '47f8d9a9-17eb-4b58-a0e3-fbc6a00d18db', display_name: 'Applicant', email: 'member@example.test',
  tenant_id: source.id, role: 'member', email_verified: true, is_active: true, created_at: '2026-10-25T08:00:00Z',
}
export const pending: AdmissionDetail = {
  id: 'f68765b7-0926-46c6-804e-3349a5f7e468', status: 'pending', source_tenant_id: source.id, target_tenant: target,
  method: 'invitation', created_at: '2026-10-20T12:00:00Z', decided_at: null, decision_reason: null, transfer: null,
  applicant: { id: member.id, email: member.email }, source_tenant: source, decided_by_user_id: null,
}
export const completedOnboarding: OnboardingStatus = {
  exists: true, status: 'completed', current_step: 'completed', entry_mode: null,
  personal_assistant_agent_id: null, completed_at: '2026-10-25T08:00:00Z',
}
export const accessEndpoint = '**/api/company-admissions/mine/page*'
export const restrictedLabels = ['Inbox', 'Admissions', 'Settings']
export const workspaceLabels = ['Agents', 'Plaza', 'OKR', 'Directory', ...restrictedLabels]

export function workspaceRequests(calls: readonly string[]) {
  return calls.filter((path) => /^\/api\/(agents|onboarding|plaza|okr|org|departments)(\/|$)/.test(path))
}

export async function prepareAccess(page: Page, initial: AdmissionDetail = pending, signedIn = true) {
  let admission = initial
  const calls: string[] = []
  if (signedIn) await page.addInitScript((key) => {
    if (!localStorage.getItem(key)) localStorage.setItem(key, 'source-token')
  }, tokenKey)
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    calls.push(path)
    const targetSession = request.headers().authorization === 'Bearer target-token'
    const publicEndpoint = ['/api/auth/providers', '/api/auth/registration-config', '/api/auth/check-duplicate', '/api/member/auth/register/init'].includes(path)
    expect(request.headers().authorization).toBe(publicEndpoint ? undefined : `Bearer ${targetSession ? 'target-token' : 'source-token'}`)
    expect(request.method()).toBe(['/api/member/auth/register/init', '/api/auth/switch-tenant'].includes(path) ? 'POST' : 'GET')
    switch (path) {
      case '/api/member/auth/me':
        return route.fulfill({ json: { ...member, tenant_id: targetSession ? target.id : source.id } })
      case '/api/tenants/me': return route.fulfill({ json: targetSession ? target : source })
      case '/api/company-admissions/mine/page': {
        expect(request.method()).toBe('GET')
        const status = url.searchParams.get('status')
        expect(url.searchParams.get('limit')).toBe(status === 'pending' ? '100' : '50')
        return route.fulfill({ json: { items: status === null || status === admission.status ? [admission] : [], next_cursor: null } })
      }
      case `/api/company-admissions/${pending.id}`: return route.fulfill({ json: admission })
      case '/api/company-admissions/transfers/mine':
      case '/api/auth/providers':
      case '/api/auth/my-tenants':
      case '/api/notifications':
      case '/api/agents/': return route.fulfill({ json: [] })
      case '/api/notifications/unread-count': return route.fulfill({ json: { unread_count: 0 } })
      case '/api/onboarding/status': return route.fulfill({ json: completedOnboarding })
      case '/api/auth/switch-tenant':
        expect(request.method()).toBe('POST')
        expect(request.postDataJSON()).toEqual({ tenant_id: target.id })
        return route.fulfill({ json: { access_token: 'target-token' } })
      case '/api/auth/registration-config': return route.fulfill({ json: { invitation_code_required: false } })
      case '/api/auth/check-duplicate': return route.fulfill({ json: { email_exists: false, has_conflict: false } })
      case '/api/member/auth/register/init':
        expect(request.method()).toBe('POST')
        return route.fulfill({ json: {
          access_token: 'source-token', user: member, admission_request: admission,
          email: member.email, user_id: member.id, message: 'Registered',
        } })
      default: throw new Error(`Unexpected admission access API request: ${request.method()} ${path}`)
    }
  })
  return { calls, setAdmission: (next: AdmissionDetail) => { admission = next } }
}
