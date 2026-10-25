import { expect, type Page } from '@playwright/test'
import { z } from 'zod'

import {
  originalCompany, originalToken, originalUser, prepareWorkspace, replacementCompany, replacementToken, replacementUser,
} from './session-fencing-fixture'

export const orgDepartment = { id: '9ddf213c-41cf-4bce-ad6d-363c2612e750', name: 'Engineering' }
export const siblingDepartment = { id: '8020bd61-452d-42cf-9695-5752abc129a2', name: 'Customer experience and business operations' }
export const directoryPath = '/app/directory?tab=departments'
export const orgMembers = [
  { membership_id: '0c929328-fb2a-4b51-83fc-284bf606a040', display_name: 'Alex Morgan' },
  { membership_id: '91efb1f0-503b-4bb4-8a40-4792f8a8c029', display_name: 'Alex Morgan' },
  { membership_id: '169eb789-1689-49c0-aa96-5da877a58036', display_name: null },
  { membership_id: '1dbec938-cfca-4c75-8dbf-3df663483b89', display_name: '   ' },
]

export async function prepareOrgDepartments(page: Page, role = 'member', switchToken = replacementToken) {
  await prepareWorkspace(page, switchToken === originalToken ? { ...originalUser, tenant_id: replacementCompany.id } : replacementUser, switchToken)
  const patches: { name: string }[] = []
  const rosterRequests: string[] = []
  const state = {
    departments: [orgDepartment, siblingDepartment],
    optionsRequests: 0,
    privilegedRequests: 0,
    patches,
    rosterRequests,
  }
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    switch (url.pathname) {
      case '/api/member/auth/me':
        if (role === 'member') return route.fallback()
        await route.fulfill({ json: { ...originalUser, role } })
        return
      case '/api/org/users':
        await route.fulfill({ json: [] })
        return
      case '/api/departments':
        state.privilegedRequests++
        await route.fulfill({ status: 403, json: { detail: 'Privileged department list' } })
        return
      case '/api/departments/options':
        expect(request.method()).toBe('GET')
        expect(url.search).toBe('')
        state.optionsRequests++
        await route.fulfill({ json: { departments: state.departments } })
        return
      case `/api/departments/${orgDepartment.id}/members`:
      case `/api/departments/${siblingDepartment.id}/members`:
        expect(request.method()).toBe('GET')
        expect(url.search).toBe('')
        state.rosterRequests.push(url.pathname)
        await route.fulfill({ json: { members: url.pathname.includes(orgDepartment.id) ? orgMembers.map((member) => ({
          ...member, email: 'private-roster@example.test', username: 'private-roster', user_id: 'private-roster-user',
        })) : [] } })
        return
      case `/api/departments/${orgDepartment.id}`: {
        expect(request.method()).toBe('PATCH')
        expect(role).toBe('org_admin')
        const patch = z.object({ name: z.string() }).strict().parse(request.postDataJSON())
        state.patches.push(patch)
        const updated = { ...orgDepartment, name: patch.name }
        state.departments = state.departments.map((department) => department.id === updated.id ? updated : department)
        await route.fulfill({ json: {
          ...updated, tenant_id: originalCompany.id, created_at: '2026-10-18T00:00:00Z',
        } })
        return
      }
      default:
        await route.fallback()
    }
  })
  return state
}
