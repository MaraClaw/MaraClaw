import { expect, type Page } from '@playwright/test'

import type { AdmissionDetail } from '../src/lib/types/company-admissions'
import { originalCompany, originalUser, prepareWorkspace, replacementCompany } from './session-fencing-fixture'

export const pendingAdmission: AdmissionDetail = {
  id: 'withdraw-request', status: 'pending', source_tenant_id: originalCompany.id, target_tenant: replacementCompany,
  method: 'invitation', created_at: '2026-10-20T12:00:00Z', decided_at: null, decision_reason: null,
  applicant: { id: originalUser.id, email: originalUser.email }, source_tenant: originalCompany, decided_by_user_id: null,
}
export const withdrawnAdmission: AdmissionDetail = { ...pendingAdmission, status: 'withdrawn', decided_at: '2026-10-21T12:00:00Z' }
export const admissionPath = `/api/company-admissions/${pendingAdmission.id}`
export const admissionPage = `/app/admissions/${pendingAdmission.id}`

export async function prepareAdmission(page: Page, initial: AdmissionDetail = pendingAdmission) {
  // prepareWorkspace owns a catch-all API route: no request can reach an engine.
  await prepareWorkspace(page)
  let detail = initial
  const calls: string[] = []
  await page.route('**/api/company-admissions/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    calls.push(`${request.method()} ${path}`)
    switch (path) {
      case `${admissionPath}/withdraw`:
        expect(request.method()).toBe('POST')
        expect(request.postData()).toBeNull()
        detail = withdrawnAdmission
        return route.fulfill({ json: detail })
      case admissionPath:
        expect(request.method()).toBe('GET')
        return route.fulfill({ json: detail })
      case '/api/company-admissions/mine':
        return route.fulfill({ json: [detail] })
      default:
        throw new Error(`Unsupported applicant fixture endpoint: ${path}`)
    }
  })
  return { calls, setDetail: (next: AdmissionDetail) => { detail = next } }
}
