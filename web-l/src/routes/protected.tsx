import { Loader2 } from 'lucide-react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { useAuth } from '@/hooks/use-auth'

export function ProtectedRoute() {
  const { status, mustChangePassword, needsOrgConfirm } = useAuth()
  const location = useLocation()

  if (status === 'loading') {
    return (
      <div className="flex min-h-svh items-center justify-center bg-background">
        <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          Loading workspace…
        </div>
      </div>
    )
  }

  if (status !== 'authenticated') {
    return <Navigate to="/login" replace state={{ from: location }} />
  }

  if (needsOrgConfirm) {
    return <Navigate to="/join" replace />
  }

  const path = location.pathname
  const allowedWhileForced =
    path === '/app/account' || path.startsWith('/app/account/') || path === '/app/settings'
  if (mustChangePassword && !allowedWhileForced) {
    return <Navigate to="/app/account" replace state={{ from: location, forcePasswordChange: true }} />
  }

  return <Outlet />
}
