import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { useAuth } from '@/hooks/use-auth'
import { switchTenant } from '@/lib/auth-api'
import type { JoinOrgResponse } from '@/lib/types/auth'

export function useJoinedWorkspace() {
  const { owner, isCurrent, refreshReplacement } = useAuth()
  const navigate = useNavigate()
  return useCallback(async (result: JoinOrgResponse) => {
    if (!isCurrent(owner)) return
    let token: string | null
    if ('request' in result) {
      switch (result.status) {
        case 'pending': return
        case 'approved': {
          const session = await switchTenant(result.request.target_tenant.id, owner)
          if (!isCurrent(owner)) return
          token = session.access_token
          break
        }
      }
    } else token = result.access_token
    const installedOwner = await refreshReplacement(owner, token)
    if (!installedOwner || !isCurrent(installedOwner)) return
    toast.success('Organization joined')
    navigate('/app', { replace: true })
  }, [owner, isCurrent, refreshReplacement, navigate])
}
