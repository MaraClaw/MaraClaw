import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { Loader2 } from 'lucide-react'
import type { ReactNode } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { getOnboardingStatus, wasOnboardingSkipped } from '@/lib/workspace-api'
import { agentExistenceOptions } from '@/lib/agents-query'
import { Button } from '@/components/ui/button'

export function OnboardingGate({ children }: { readonly children?: ReactNode }) {
  const scope = useWorkspaceSession()
  const location = useLocation()
  const agents = useQuery(agentExistenceOptions())
  const onboarding = useQuery({ queryKey: ['onboarding'], queryFn: getOnboardingStatus })

  if (agents.isLoading || onboarding.isLoading) {
    return (
      <div className="flex h-40 items-center justify-center text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden />
      </div>
    )
  }

  const hasAgents = (agents.data?.items.length ?? 0) > 0
  const completed = onboarding.data?.status === 'completed'
  const skip = wasOnboardingSkipped()
  const onOnboarding = location.pathname.startsWith('/app/onboarding')
  if (!hasAgents && !completed && !skip && !onOnboarding && (agents.isError || onboarding.isError)) {
    return <div className="space-y-3 p-6">
      <p role="alert" className="text-sm text-destructive">Could not check onboarding. Try again.</p>
      <Button variant="outline" disabled={agents.isFetching || onboarding.isFetching} onClick={() => {
        void agents.refetch()
        void onboarding.refetch()
      }}>Retry onboarding check</Button>
    </div>
  }
  if (scope.isCurrent() && agents.isSuccess && onboarding.isSuccess && !hasAgents && !completed && !skip && !onOnboarding) {
    return <Navigate to="/app/onboarding" replace />
  }

  return children ?? <Outlet />
}
