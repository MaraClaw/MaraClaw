import { queryOptions, type QueryClient } from '@tanstack/react-query'

import { fetchInvitationCodes, revokeInvitation, sendInvitations } from '../lib/invitations-api.ts'

export const invitationKeys = {
  tenant: (tenant: string | null) => ['admin-invitations', tenant] as const,
  list: (tenant: string | null, page: number) => ['admin-invitations', tenant, page] as const,
}

export function invitationHistoryOptions(tenant: string | null, page: number, enabled: boolean) {
  return queryOptions({
    queryKey: invitationKeys.list(tenant, page),
    queryFn: ({ signal }) => fetchInvitationCodes(page, signal),
    enabled: enabled && tenant !== null,
    retry: false,
  })
}

export function sendInvitationOptions(client: QueryClient, tenant: string | null) {
  return {
    mutationFn: sendInvitations,
    retry: false,
    onSuccess: () => { void client.invalidateQueries({ queryKey: invitationKeys.tenant(tenant) }) },
  }
}

export function revokeInvitationOptions(client: QueryClient, tenant: string | null) {
  return {
    mutationFn: revokeInvitation,
    retry: false,
    onSuccess: () => { void client.invalidateQueries({ queryKey: invitationKeys.tenant(tenant) }) },
  }
}
