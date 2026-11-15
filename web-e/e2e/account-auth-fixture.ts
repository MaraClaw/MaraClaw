import type { Page, Route } from '@playwright/test'

import type { UserOut } from '../src/lib/types/auth'

export const member: UserOut = {
  id: 'f936188e-a374-4edf-9b05-ac6c9cb236a6', email: 'member@example.test',
  display_name: 'Member', role: 'member', tenant_id: 'f0c4b91d-7d55-4d66-845c-320f76e938de', email_verified: true,
  must_change_password: true, is_active: true, created_at: '2026-10-25T08:00:00Z',
}

export async function prepareAuth(page: Page, options: {
  readonly invitationRequired?: boolean
  readonly checkDuplicate?: (route: Route) => Promise<void>
} = {}) {
  const logins: unknown[] = []
  const registrations: unknown[] = []
  const availability: string[][][] = []
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    switch (url.pathname) {
      case '/api/auth/providers':
      case '/api/auth/my-tenants':
        await route.fulfill({ json: [] })
        break
      case '/api/auth/registration-config':
        await route.fulfill({ json: { invitation_code_required: options.invitationRequired ?? false } })
        break
      case '/api/auth/check-duplicate':
        availability.push([...url.searchParams])
        if (options.checkDuplicate) await options.checkDuplicate(route)
        else await route.fulfill({ json: { email_exists: false, has_conflict: false } })
        break
      case '/api/member/auth/register/init':
        registrations.push(request.postDataJSON())
        await route.fulfill({ json: {
          access_token: 'registered-account', user: member, email: member.email, user_id: member.id, message: 'Registration complete',
        } })
        break
      case '/api/member/auth/login':
        logins.push(request.postDataJSON())
        await route.fulfill({ json: { access_token: 'email-account', user: member } })
        break
      case '/api/member/auth/me':
        await route.fulfill({ json: member })
        break
      case '/api/notifications/unread-count':
        await route.fulfill({ json: { unread_count: 0 } })
        break
      case '/api/company-admissions/mine/page':
        await route.fulfill({ json: { items: [], next_cursor: null } })
        break
      case '/api/tenants/me':
        await route.fulfill({ json: { id: member.tenant_id, name: 'Company', slug: 'company' } })
        break
      default:
        throw new Error(`Unexpected auth request: ${request.method()} ${url.pathname}`)
    }
  })
  return { logins, registrations, availability, errors }
}
