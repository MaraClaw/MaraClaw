export type SuggestedOrg = {
  id: string
  name: string
  slug: string
}

export type UserOut = {
  id: string
  email: string | null
  display_name: string
  role: string
  tenant_id: string | null
  email_verified?: boolean
}

export type EmailLookupResponse = {
  match: SuggestedOrg | null
  fallback: SuggestedOrg | null
}

export type AuthResponse = {
  access_token: string
  user: UserOut
  needs_company_setup?: boolean
  needs_org_confirm?: boolean
  suggested_org?: SuggestedOrg | null
  message?: string
}
