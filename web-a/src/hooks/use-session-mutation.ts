import { useMutation } from '@tanstack/react-query'

import { adminSession } from '@/lib/admin-session'
import { useAuth } from './use-auth'
import { ownMutationOptions, type SessionMutationOptions } from './session-mutation'

export function useSessionMutation<TData = unknown, TError = Error, TVariables = void, TContext = unknown>(
  options: SessionMutationOptions<TData, TError, TVariables, TContext>,
) {
  const { owner } = useAuth()
  return useMutation(ownMutationOptions(adminSession, owner, options))
}
