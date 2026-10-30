import { expect, type Page } from '@playwright/test'

import type { DirectoryUser } from '../src/lib/directory-api'
import { prepareOrgDepartments } from './org-departments-fixture'
import { originalToken, originalUser, replacementToken } from './session-fencing-fixture'

export const dana = {
  ...originalUser, display_name: 'Dana Woods', email: 'dana@example.test', title: 'Product engineer', role: 'org_admin',
} satisfies DirectoryUser
export const rowan = {
  ...originalUser, id: '49926b42-c074-4aa1-9aa1-54f0adbf70b9',
  display_name: 'Rowan Chen', email: 'rowan@example.test', title: 'Marketing designer',
} satisfies DirectoryUser
export const people = [dana, rowan] as const
export const peoplePageRoute = /\/api\/org\/users\/page(?:\?|$)/

export async function prepareDirectory(page: Page, role: 'org_admin' | 'member' = 'org_admin') {
  const state = await prepareOrgDepartments(page, role)
  const retiredRequests: string[] = []
  const requests: URL[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname.startsWith('/api/enterprise/org/') || url.pathname === '/api/org/users') retiredRequests.push(url.pathname)
    if (url.pathname !== '/api/org/users/page') return
    requests.push(url)
    expect(request.method()).toBe('GET')
    expect(request.postData()).toBeNull()
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(request.headers().authorization)
    expect(url.searchParams.get('limit')).toBe('50')
    const search = url.searchParams.get('search')
    expect(search).not.toBeNull()
    expect(search).toBe(search?.trim().toLowerCase())
  })
  await page.route(peoplePageRoute, (route) => {
    const search = new URL(route.request().url()).searchParams.get('search') ?? ''
    const items = people.filter((person) => [person.display_name, person.email, person.title, person.role]
      .some((value) => value?.toLowerCase().includes(search)))
    return route.fulfill({ json: { items, next_cursor: null } })
  })
  return { state, retiredRequests, requests }
}
