import { AdminShell } from '@/components/layout/admin-shell'
import { SessionQueryProvider } from '@/hooks/session-query-provider'

/** Layout route below `ProtectedRoute`: the shell and the session Query scope download together, only once someone is signed in. */
export function SessionShell() {
  return (
    <SessionQueryProvider>
      <AdminShell />
    </SessionQueryProvider>
  )
}
