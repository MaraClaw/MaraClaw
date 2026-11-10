import type { ReactNode } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { RequestErrorToast } from '@/components/request-error-toast'
import { OnboardingGate } from '@/components/layout/onboarding-gate'
import { Button } from '@/components/ui/button'
import { useAdmissionAccess } from '@/hooks/use-admission-access'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { isAdmissionAccessPath } from '@/lib/admission-access'

export function AdmissionAccessGate() {
  const { pathname } = useLocation()
  const query = useAdmissionAccess()
  const scope = useWorkspaceSession()
  if (isAdmissionAccessPath(pathname)) return <Outlet />
  if (query.isPending) {
    return <p role="status" className="p-6 text-sm text-muted-foreground">Checking organization access…</p>
  }
  if (query.isError) {
    return <div className="space-y-3 p-6">
      <RequestErrorToast error={query.error} message="Unable to confirm organization access. Retry to continue to the workspace." />
      <Button variant="outline" disabled={query.isFetching} onClick={scope.effect(() => { void query.refetch() })}>Retry access check</Button>
    </div>
  }
  if (!query.workspaceAllowed) return <Navigate to="/app/notifications" replace />
  return <Outlet />
}

export function AdmissionInboxGate({ children }: { readonly children: ReactNode }) {
  const { workspaceAllowed } = useAdmissionAccess()
  return workspaceAllowed ? <OnboardingGate>{children}</OnboardingGate> : children
}
