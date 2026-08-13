import { Loader2 } from 'lucide-react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { useAuth } from '@/hooks/use-auth'

export function ProtectedRoute() {
  const { status, isAdmin, mustChangePassword } = useAuth()
  const location = useLocation()

  if (status === 'loading') {
    return (
      <div className="flex min-h-svh items-center justify-center bg-background">
        <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          Loading console…
        </div>
      </div>
    )
  }

  if (status !== 'authenticated' || !isAdmin) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }

  const onAccount = location.pathname === '/account' || location.pathname.startsWith('/account/')
  if (mustChangePassword && !onAccount) {
    return <Navigate to="/account" replace state={{ from: location, forcePasswordChange: true }} />
  }

  return <Outlet />
}
