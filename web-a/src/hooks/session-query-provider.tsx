import { QueryClientProvider } from '@tanstack/react-query'
import { useEffect, useMemo, type ReactNode } from 'react'

import { useAuth } from '@/hooks/use-auth'
import { adminSession } from '@/lib/admin-session'
import { createSessionQueryScope, ownQueryClient } from './session-query-client'

/** One Query client per session owner, remounted on every generation so a retired session's cache never reaches the next. */
export function SessionQueryProvider({ children }: { children: ReactNode }) {
  const { owner } = useAuth()
  const { client } = useMemo(() => createSessionQueryScope(owner), [owner])

  useEffect(() => ownQueryClient(adminSession, owner, client), [owner, client])

  return (
    <QueryClientProvider key={owner.generation} client={client}>
      {children}
    </QueryClientProvider>
  )
}
