import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useAuth } from '@/hooks/use-auth'
import { admissionAccessOptions } from '@/lib/admission-access'

export function useAdmissionAccess() {
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  const tenantId = user?.tenant_id ?? null
  const query = useQuery(admissionAccessOptions(scope.owner, tenantId))
  return { ...query, workspaceAllowed: tenantId !== null && query.isSuccess && query.data === false }
}
