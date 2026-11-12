import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
import { Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { CompanyStatusIcon } from '@/components/companies/company-status-icon'
import { CreateCompanyForm } from '@/components/companies/create-company-form'
import { Badge } from '@/components/ui/badge'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { companiesPageOptions } from '@/hooks/company-options'
import { useAuth } from '@/hooks/use-auth'
import { useSessionMutation as useMutation } from '@/hooks/use-session-mutation'
import { toggleCompany } from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import { isPlatformAdminUser } from '@/lib/types/auth'
import { useTranslation } from '@/hooks/use-translation'

function useDebouncedValue(value: string, delayMs: number): string {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(timer)
  }, [value, delayMs])
  return debounced
}

export function CompaniesPage() {
  const { t, number } = useTranslation()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const canCreate = isPlatformAdminUser(user)
  const [searchInput, setSearchInput] = useState('')
  const search = useDebouncedValue(searchInput, 300)
  const companies = useInfiniteQuery(companiesPageOptions(user?.tenant_id ?? null, search))
  const rows = companies.data?.pages.flatMap((page) => page.items) ?? []
  const toggle = useMutation({
    mutationFn: toggleCompany,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-companies'] })
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : t('Could not update company'))
    },
  })

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
      <div>
        <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Companies')}</h1>
        <p className="mt-2 text-muted-foreground">
          {t('System organizations and customer companies. Email domains are managed on each company.')}
        </p>
      </div>

      {canCreate ? (
        <CreateCompanyForm
          onCreated={(name, adminEmail) => {
            void queryClient.invalidateQueries({ queryKey: ['admin-companies'] })
            toast.success(t('Created {name}. {email} must change password on first login.', { name, email: adminEmail }))
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
          placeholder={t('Search companies by name')}
          aria-label={t('Search companies by name')}
          className="pl-10"
        />
      </div>

      {companies.isLoading ? <p className="text-sm text-muted-foreground">{t('Loading companies…')}</p> : null}
      {companies.error ? (
        <ErrorToast error={companies.error} message={companies.error instanceof ApiError ? companies.error.message : t('Failed to load companies')} />
      ) : null}

      {!companies.isLoading && !companies.error && rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {search.trim()
            ? t('No companies match “{search}”.', { search: search.trim() })
            : t('No companies yet.')}
        </p>
      ) : null}

      <div className="grid gap-4">
        {rows.map((company) => (
          <Card key={company.id} className="relative transition-colors hover:bg-muted/40">
            <Link
              to={`/companies/${company.id}`}
              aria-label={t('Open {name}', { name: company.name })}
              className="absolute inset-0 z-10 rounded-[inherit] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            />
            <CardHeader className="flex flex-row items-start justify-between gap-4">
              <div>
                <CardTitle className="flex items-center gap-2.5">
                  <CompanyStatusIcon active={company.is_active} />
                  {company.name}
                </CardTitle>
                <CardDescription>
                  {company.slug}
                  {company.org_admin_email ? ` · ${company.org_admin_email}` : ''}
                </CardDescription>
              </div>
              <div className="flex flex-wrap items-center gap-2">
              {company.is_system ? <Badge variant="secondary">{t('System')}</Badge> : null}
              {company.is_default_end_user_org ? <Badge>{t('Default for end users')}</Badge> : null}
                <Badge variant={company.is_active ? 'success' : 'destructive'}>
                {t(company.is_active ? 'Active' : 'Disabled')}
                </Badge>
              </div>
            </CardHeader>
            <CardContent className="flex flex-wrap items-center gap-3">
              <span className="text-sm text-muted-foreground">
                {t('{users} users · {agents} agents', { users: number(company.user_count), agents: number(company.agent_count) })}
              </span>
              {canCreate && company.can_disable ? (
                <Button
                  variant="ghost"
                  size="sm"
                  className="relative z-20"
                  disabled={toggle.isPending}
                  onClick={() => toggle.mutate(company.id)}
                >
                  {t(company.is_active ? 'Disable' : 'Enable')}
                </Button>
              ) : null}
            </CardContent>
          </Card>
        ))}
      </div>
      {companies.hasNextPage ? (
        <Button
          type="button"
          variant="outline"
          disabled={companies.isFetching}
          aria-busy={companies.isFetchingNextPage}
          onClick={() => { void companies.fetchNextPage() }}
        >
          {t('Load more companies')}
        </Button>
      ) : null}
    </div>
  )
}
