import { expect, type Page } from '@playwright/test'
import type { AdmissionDetail } from '../src/lib/types/company-admissions'
import type { TransferQuote, TransferSummary } from '../src/lib/types/company-transfers'

export const tokenKey = 'maraclaw-enduser-token'
export const source = { id: 'bdd07e40-83a6-48f3-9049-5ffae4774f75', name: 'OpenClaw', slug: 'openclaw' }
export const target = { id: 'c919ad90-90be-4a1d-b699-1d02138b7f45', name: 'Synthetic Company', slug: 'synthetic' }
export const member = { id: '47f8d9a9-17eb-4b58-a0e3-fbc6a00d18db', display_name: 'Synthetic source member', email: 'member@example.test', tenant_id: source.id, role: 'member', email_verified: true }
export const quote: TransferQuote = {
  fee_amount_minor: 123, currency: 'JPY', instructions: 'Synthetic current policy', source_eligible: true,
  has_owned_agents: true, explicit_choice_required: true, free_requires_destructive_consent: true,
}
export const payment: TransferSummary = {
  id: '3bf6007f-8e95-416d-bd6c-e2c72754edb7', applicant_user_id: member.id,
  source_tenant_id: source.id, target_tenant_id: target.id, choice: 'paid', status: 'payment_review',
  fee_amount_minor: 1234, currency: 'KWD', instructions: 'Synthetic saved instructions\nReference: request-1234',
  created_at: '2026-10-24T12:00:00Z', free_consent_at: null, payment_confirmed_at: null,
  reviewed_at: null, reviewed_by_user_id: null, admission_request_id: null, review_reason: null,
}
export const admission: AdmissionDetail = {
  id: '4b306b7c-cbe4-49c2-9b5f-1f98a009395a', status: 'pending', source_tenant_id: source.id,
  target_tenant: target, method: 'invitation', created_at: payment.created_at, decided_at: null,
  decision_reason: null, applicant: { id: member.id, email: member.email }, source_tenant: source,
  decided_by_user_id: null, transfer: { transfer_request_id: payment.id, choice: 'free', fee_amount_minor: 0, currency: 'JPY', free_consent_at: payment.created_at },
}

export async function prepareTransfers(page: Page, options: {
  readonly eligible?: boolean
  readonly saved?: TransferSummary
  readonly destination?: AdmissionDetail
} = {}) {
  const state: { transfer: TransferSummary | null; destination: AdmissionDetail | null; submissions: unknown[]; switches: number } = {
    transfer: options.saved ?? null, destination: options.destination ?? null, submissions: [], switches: 0,
  }
  await page.addInitScript((key) => { if (!localStorage.getItem(key)) localStorage.setItem(key, 'source-token') }, tokenKey)
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const replacing = request.headers().authorization === 'Bearer replacement-token'
    expect(request.headers().authorization).toBe(`Bearer ${replacing ? 'replacement-token' : 'source-token'}`)
    expect(request.method()).toBe(['/api/tenants/transfer', `/api/company-admissions/transfers/${payment.id}/withdraw`].includes(url.pathname) ? 'POST' : 'GET')
    switch (url.pathname) {
      case '/api/auth/me':
        await route.fulfill({ json: replacing ? { ...member, id: '49926b42-c074-4aa1-9aa1-54f0adbf70b9', display_name: 'Replacement member' } : member }); return
      case '/api/tenants/me': await route.fulfill({ json: source }); return
      case '/api/tenants/lookup-by-email': await route.fulfill({ json: { match: target, fallback: source } }); return
      case '/api/company-admissions/transfer-quote': await route.fulfill({ json: { ...quote, source_eligible: options.eligible ?? true } }); return
      case '/api/tenants/transfer': {
        const body: unknown = request.postDataJSON()
        state.submissions.push(body)
        expect(body).toEqual(expect.objectContaining({ password: 'synthetic-password' }))
        if (typeof body === 'object' && body !== null && 'transfer_choice' in body && body.transfer_choice === 'paid') {
          state.transfer = payment
          await route.fulfill({ status: 202, json: { status: 'payment_review', transfer_request: payment } })
        } else {
          state.destination = admission
          await route.fulfill({ status: 202, json: { status: 'pending', request: admission } })
        }
        return
      }
      case '/api/company-admissions/transfers/mine':
        expect(url.searchParams.get('limit')).toBe('100')
        await route.fulfill({ json: replacing ? [] : state.transfer ? [state.transfer] : [] }); return
      case `/api/company-admissions/transfers/${payment.id}`:
        await route.fulfill(replacing ? { status: 404, json: { detail: 'Not found for replacement member' } } : { json: state.transfer }); return
      case `/api/company-admissions/transfers/${payment.id}/withdraw`:
        expect(request.postData()).toBeNull()
        expect(state.transfer?.status).toBe('payment_review')
        state.transfer = { ...(state.transfer ?? payment), status: 'withdrawn' }
        await route.fulfill({ json: state.transfer }); return
      case '/api/company-admissions/mine/page': await route.fulfill({ json: { items: state.destination ? [state.destination] : [], next_cursor: null } }); return
      case `/api/company-admissions/${admission.id}`: await route.fulfill({ json: state.destination }); return
      case '/api/notifications/unread-count': await route.fulfill({ json: { unread_count: 0 } }); return
      case '/api/onboarding/status': await route.fulfill({ json: { exists: true, status: 'completed', current_step: 'completed' } }); return
      case '/api/agents/': await route.fulfill({ json: [] }); return
      case '/api/auth/switch-tenant': state.switches++; break
      default: break
    }
    await route.fulfill({ status: 501, json: { detail: 'Unexpected synthetic endpoint' } })
    expect(url.pathname, 'Unexpected synthetic endpoint').toBe('supported transfer fixture route')
  })
  await page.routeWebSocket('**/ws/**', (socket) => socket.close({ code: 4003, reason: 'Synthetic transfers only' }))
  return state
}

export async function openTransferChoice(page: Page, method: 'invite' | 'domain' = 'invite') {
  await page.goto('/transfer')
  await page.getByLabel('Current password').fill('synthetic-password')
  if (method === 'invite') await page.getByLabel('Invitation code', { exact: true }).fill('synthetic-invite')
  await page.getByRole('button', { name: method === 'invite' ? 'Transfer with invite' : `Transfer to ${target.name}`, exact: true }).click()
  await expect(page.getByRole('alertdialog')).toBeVisible()
}
