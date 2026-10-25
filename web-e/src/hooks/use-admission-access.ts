import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useAuth } from '@/hooks/use-auth'
import { hasPendingAdmission } from '@/lib/admission-access'

export function useAdmissionAccess() {
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  const tenantId = user?.tenant_id ?? null
  const query = useQuery({
    queryKey: ['company-admissions', 'workspace-access', tenantId],
    queryFn: ({ signal }) => hasPendingAdmission(tenantId, { owner: scope.owner, signal }),
    retry: false,
    refetchInterval: (query) => query.state.data === true ? 30_000 : false,
  })
  return { ...query, workspaceAllowed: query.isSuccess && query.data === false }
}
