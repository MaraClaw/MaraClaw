import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Building2, Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { CreateCompanyForm } from '@/components/companies/create-company-form'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { useAuth } from '@/hooks/use-auth'
import { getTenant, listCompanies, toggleCompany } from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import { isPlatformAdminUser } from '@/lib/types/auth'

function useDebouncedValue(value: string, delayMs: number): string {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(timer)
  }, [value, delayMs])
  return debounced
}

export function CompaniesPage() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const canCreate = isPlatformAdminUser(user)
  const [searchInput, setSearchInput] = useState('')
  const search = useDebouncedValue(searchInput, 300)
  const companies = useQuery({
    queryKey: ['admin-companies', user?.tenant_id, search],
    queryFn: async () => {
      try {
        return await listCompanies(search)
      } catch (error) {
        if (error instanceof ApiError && error.status === 403 && user?.tenant_id) {
          const own = await getTenant(user.tenant_id)
          const needle = search.trim().toLowerCase()
          if (!needle) return [own]
          const haystack = `${own.name} ${own.slug}`.toLowerCase()
          return haystack.includes(needle) ? [own] : []
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

      {canCreate ? (
        <CreateCompanyForm
          onCreated={(name, adminEmail) => {
            void queryClient.invalidateQueries({ queryKey: ['admin-companies'] })
            toast.success(`Created ${name}. ${adminEmail} must change password on first login.`)
          }}
        />
      ) : null}

      <div className="relative">
        <Search
          className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden
        />
        <Input
          type="search"
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
          placeholder="Search companies by name"
          aria-label="Search companies by name"
          className="pl-10"
        />
      </div>

      {companies.isLoading ? <p className="text-sm text-muted-foreground">Loading companies…</p> : null}
      {companies.error ? (
        <p className="text-sm text-destructive">
          {companies.error instanceof ApiError ? companies.error.message : 'Failed to load companies'}
        </p>
      ) : null}

      {!companies.isLoading && (companies.data ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {search.trim()
            ? `No companies match “${search.trim()}”.`
            : 'No companies yet.'}
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
                <Badge variant={company.is_active ? 'success' : 'destructive'}>
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
              {canCreate && company.can_disable ? (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={toggle.isPending}
                  onClick={() => toggle.mutate(company.id)}
                >
                  {company.is_active ? 'Disable' : 'Enable'}
                </Button>
              ) : null}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
