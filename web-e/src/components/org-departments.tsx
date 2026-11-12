import { useTranslation } from '@/hooks/use-translation'
import { OrgDepartmentCard } from '@/components/org-department-card'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { listOrgDepartments } from '@/lib/org-departments-api'

import { RequestErrorToast } from '@/components/request-error-toast'
export function OrgDepartments() {
  const { t: translate } = useTranslation()
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  const departments = useQuery({
    queryKey: ['local-departments'],
    queryFn: ({ signal }) => listOrgDepartments(signal),
  })

  return (
    <section aria-labelledby="local-departments-heading" className="space-y-4">
      <div className="space-y-1">
        <h2 id="local-departments-heading" className="font-display text-lg font-semibold">{translate("Org departments")}</h2>
        <p className="text-sm text-muted-foreground">{translate("Departments managed in this company.")}</p>
      </div>
      {departments.isError ? (
        <div className="space-y-3">
          <RequestErrorToast error={departments.error} message={userFacingRequestError(departments.error, translate("Unable to load org departments. Try again."))} />
          <Button variant="outline" disabled={departments.isFetching} onClick={scope.effect(() => { void departments.refetch() })}>{translate("Retry")}</Button>
        </div>
      ) : null}
      {departments.isPending ? (
        <p role="status" className="text-sm text-muted-foreground">{translate("Loading org departments...")}</p>
      ) : departments.data?.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {translate("No org departments yet.")}{' '}{user?.role === 'org_admin'
            ? translate("Add departments in the admin console on Users.")
            : translate("Ask an org admin to add departments.")}
        </p>
      ) : departments.data ? (
        <ul className="grid grid-cols-1 items-start gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {departments.data.map((department) => (
            <li key={department.id} className="min-w-0" aria-label={department.name}>
              <OrgDepartmentCard department={department} canEdit={user?.role === 'org_admin'} />
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  )
}
