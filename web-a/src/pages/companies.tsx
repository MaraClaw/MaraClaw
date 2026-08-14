import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Building2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { getTenant, listCompanies, toggleCompany } from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import { useAuth } from '@/hooks/use-auth'

export function CompaniesPage() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const companies = useQuery({
    queryKey: ['admin-companies', user?.tenant_id],
    queryFn: async () => {
      try {
        return await listCompanies()
      } catch (error) {
        if (error instanceof ApiError && error.status === 403 && user?.tenant_id) {
          return [await getTenant(user.tenant_id)]
        }
        throw error
      }
    },
  })
  const toggle = useMutation({
    mutationFn: toggleCompany,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-companies'] })
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Could not update company')
    },
  })

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
      <div>
        <h1 className="font-display text-3xl font-semibold tracking-tight">Companies</h1>
        <p className="mt-2 text-muted-foreground">
          System organizations and customer companies. Email domains are managed on each company.
        </p>
      </div>

      {companies.isLoading ? <p className="text-sm text-muted-foreground">Loading companies…</p> : null}
      {companies.error ? (
        <p className="text-sm text-destructive">
          {companies.error instanceof ApiError ? companies.error.message : 'Failed to load companies'}
        </p>
      ) : null}

      <div className="grid gap-4">
        {(companies.data ?? []).map((company) => (
          <Card key={company.id}>
            <CardHeader className="flex flex-row items-start justify-between gap-4">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <Building2 className="size-4" />
                  {company.name}
                </CardTitle>
                <CardDescription>
                  {company.slug}
                  {company.org_admin_email ? ` · ${company.org_admin_email}` : ''}
                </CardDescription>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {company.is_system ? <Badge variant="secondary">System</Badge> : null}
                {company.is_default_end_user_org ? <Badge>Default for end users</Badge> : null}
                <Badge variant={company.is_active ? 'outline' : 'secondary'}>
                  {company.is_active ? 'Active' : 'Disabled'}
                </Badge>
              </div>
            </CardHeader>
            <CardContent className="flex flex-wrap items-center gap-3">
              <span className="text-sm text-muted-foreground">
                {company.user_count} users · {company.agent_count} agents
              </span>
              <Button asChild variant="outline" size="sm">
                <Link to={`/companies/${company.id}`}>Email domains</Link>
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={toggle.isPending || company.is_default_end_user_org}
                onClick={() => toggle.mutate(company.id)}
              >
                {company.is_active ? 'Disable' : 'Enable'}
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
