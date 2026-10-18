import { expect, type Page } from '@playwright/test'
import { z } from 'zod'

import { originalCompany, originalUser, prepareWorkspace } from './session-fencing-fixture'

export const localDepartment = { id: '9ddf213c-41cf-4bce-ad6d-363c2612e750', name: 'Engineering' }
export const siblingDepartment = { id: '8020bd61-452d-42cf-9695-5752abc129a2', name: 'Customer experience and business operations' }
export const directoryPath = '/app/directory?tab=departments'

export async function prepareLocalDepartments(page: Page, role = 'member') {
  await prepareWorkspace(page)
  const patches: { name: string }[] = []
  const state = {
    departments: [localDepartment, siblingDepartment],
    optionsRequests: 0,
    privilegedRequests: 0,
    patches,
  }
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    switch (url.pathname) {
      case '/api/auth/me':
        if (role === 'member') return route.fallback()
        await route.fulfill({ json: { ...originalUser, role } })
        return
      case '/api/org/users':
      case '/api/enterprise/org/members':
        await route.fulfill({ json: [] })
        return
      case '/api/enterprise/org/departments':
        await route.fulfill({ json: {
          items: [{ id: 'provider-dept', name: 'Synced operations', path: 'Company / Operations', member_count: 7 }],
          total_member: 12,
        } })
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
      case `/api/departments/${localDepartment.id}`: {
        expect(request.method()).toBe('PATCH')
        expect(role).toBe('org_admin')
        const patch = z.object({ name: z.string() }).strict().parse(request.postDataJSON())
        state.patches.push(patch)
        const updated = { ...localDepartment, name: patch.name }
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
