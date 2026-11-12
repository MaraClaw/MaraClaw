import { Loader2 } from 'lucide-react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { useAuth } from '@/hooks/use-auth'
import { useTranslation } from '@/hooks/use-translation'

export function ProtectedRoute() {
  const { t } = useTranslation()
  const { status, isAdmin, mustChangePassword } = useAuth()
  const location = useLocation()

  if (status === 'loading') {
    return (
      <div className="flex min-h-svh items-center justify-center bg-background">
        <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          {t('Loading console…')}
        </div>
      </div>
    )
  }

  if (status !== 'authenticated' || !isAdmin) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }

  const path = location.pathname
  const allowedWhileForced =
    path === '/settings' ||
    path.startsWith('/settings/')
  if (mustChangePassword && !allowedWhileForced) {
    return <Navigate to="/settings" replace state={{ from: location, forcePasswordChange: true }} />
  }

  return <Outlet />
}
