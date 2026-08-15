import { useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { getOnboardingStatus, listAgents, wasOnboardingSkipped } from '@/lib/workspace-api'

export function OnboardingGate() {
  const location = useLocation()
  const agents = useQuery({ queryKey: ['agents'], queryFn: listAgents })
  const onboarding = useQuery({ queryKey: ['onboarding'], queryFn: getOnboardingStatus })

  if (agents.isLoading || onboarding.isLoading) {
    return (
      <div className="flex h-40 items-center justify-center text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden />
      </div>
    )
  }

  const hasAgents = (agents.data?.length ?? 0) > 0
  const completed = onboarding.data?.status === 'completed'
  const skip = wasOnboardingSkipped()
  const onOnboarding = location.pathname.startsWith('/app/onboarding')
  if (!hasAgents && !completed && !skip && !onOnboarding) {
    return <Navigate to="/app/onboarding" replace />
  }

  return <Outlet />
}
