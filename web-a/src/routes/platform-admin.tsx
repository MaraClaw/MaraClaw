import { Navigate, Outlet } from 'react-router-dom'

import { useAuth } from '@/hooks/use-auth'
import { isPlatformAdminUser } from '@/lib/types/auth'

export function PlatformAdminRoute() {
  const { user } = useAuth()
  if (!isPlatformAdminUser(user)) {
    return <Navigate to="/" replace />
  }
  return <Outlet />
}
