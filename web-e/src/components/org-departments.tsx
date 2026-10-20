import { OrgDepartmentCard } from '@/components/org-department-card'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/hooks/use-auth'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { userFacingRequestError } from '@/lib/http'
import { listOrgDepartments } from '@/lib/org-departments-api'

export function OrgDepartments() {
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  const departments = useQuery({
    queryKey: ['local-departments'],
    queryFn: ({ signal }) => listOrgDepartments(signal),
  })

  return (
    <section aria-labelledby="local-departments-heading" className="space-y-4">
      <div className="space-y-1">
        <h2 id="local-departments-heading" className="font-display text-lg font-semibold">Org departments</h2>
        <p className="text-sm text-muted-foreground">Departments managed in this company.</p>
      </div>
      {departments.isError ? (
        <div className="space-y-3">
          <p role="status" className="text-sm text-destructive">
            {userFacingRequestError(departments.error, 'Unable to load org departments. Try again.')}
          </p>
          <Button variant="outline" disabled={departments.isFetching} onClick={scope.effect(() => { void departments.refetch() })}>Retry</Button>
        </div>
      ) : null}
      {departments.isPending ? (
        <p role="status" className="text-sm text-muted-foreground">Loading org departments...</p>
      ) : departments.data?.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No org departments yet. {user?.role === 'org_admin'
            ? 'Add departments in the admin console on Users.'
            : 'Ask an org admin to add departments.'}
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
