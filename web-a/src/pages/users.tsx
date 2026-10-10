import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Search } from 'lucide-react'
import { useCallback, useEffect, useId, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { UserCard } from '@/components/users/user-card'
import { useAuth } from '@/hooks/use-auth'
import { useSessionMutation as useMutation } from '@/hooks/use-session-mutation'
import { listCompanies } from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import { isGenesisAdmin, isPlatformAdminUser } from '@/lib/types/auth'
import {
  asAdminUser,
  isEndUserRole,
  listPlatformAdmins,
  listUsers,
  setOrgAdminActive,
  setPlatformAdminActive,
  setUserActive,
  type AdminUser,
} from '@/lib/users-api'
import { TenantUsers } from '@/pages/users-departments'

export function UsersPage() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const platformAdmin = isPlatformAdminUser(user)
  const genesis = isGenesisAdmin(user)
  const [params, setParams] = useSearchParams()
  const search = params.get('q') ?? ''
  const companyId = params.get('company') ?? ''
  const tenantId = platformAdmin ? companyId : user?.tenant_id
  const filterId = useId()

  const updateListState = useCallback((patch: { q?: string; company?: string }, replace = true) => {
    setParams(
      (current) => {
        const next = new URLSearchParams(current)
        if (patch.q !== undefined) {
          if (patch.q) next.set('q', patch.q)
          else next.delete('q')
        }
        if (patch.company !== undefined) {
          if (patch.company) next.set('company', patch.company)
          else next.delete('company')
        }
        return next
      },
      { replace },
    )
  }, [setParams])

  const companies = useQuery({
    queryKey: ['admin-companies'],
    queryFn: ({ signal }) => listCompanies(undefined, signal),
    enabled: platformAdmin,
  })

  useEffect(() => {
    if (!platformAdmin || companyId || !companies.data?.length) return
    const own = companies.data.find((company) => company.id === user?.tenant_id)
    updateListState({ company: own?.id ?? companies.data[0].id })
  }, [platformAdmin, companyId, companies.data, user?.tenant_id, updateListState])

  useEffect(() => {
    const stored = sessionStorage.getItem('web-a:users-scroll')
    if (stored) {
      window.scrollTo(0, Number(stored))
      sessionStorage.removeItem('web-a:users-scroll')
    }
  }, [])

  const users = useQuery({
    queryKey: ['admin-users', tenantId],
    queryFn: ({ signal }) => listUsers(platformAdmin ? companyId || undefined : undefined, signal),
    enabled: !platformAdmin || Boolean(companyId),
  })

  const platformAdmins = useQuery({
    queryKey: ['admin-platform-admins'],
    queryFn: ({ signal }) => listPlatformAdmins(signal),
    enabled: platformAdmin,
  })

  const platformRows = useMemo(
    () => (platformAdmins.data ?? []).map((admin) => asAdminUser(admin)),
    [platformAdmins.data],
  )

  const visibleCompany = useMemo(
    () => [...filterUsers(users.data ?? [], search)].sort((a, b) => Number(isEndUserRole(a.role)) - Number(isEndUserRole(b.role))),
    [users.data, search],
  )
  const visiblePlatform = useMemo(() => filterUsers(platformRows, search), [platformRows, search])

  function canToggle(row: AdminUser): boolean {
    if (!user || row.id === user.id || row.is_genesis) return false
    if (isEndUserRole(row.role)) {
      return platformAdmin || Boolean(user.tenant_id && row.tenant_id === user.tenant_id)
    }
    if (row.role === 'platform_admin') {
      return genesis && platformAdmin
    }
    if (row.role === 'org_admin') {
      return genesis && user.role === 'org_admin' && Boolean(user.tenant_id && row.tenant_id === user.tenant_id)
    }
    return false
  }

  const toggle = useMutation({
    mutationFn: async ({ row, isActive }: { row: AdminUser; isActive: boolean }) => {
      if (row.role === 'platform_admin') {
        const updated = await setPlatformAdminActive(row.id, isActive)
        return asAdminUser(updated)
      }
      if (row.role === 'org_admin') {
        return setOrgAdminActive(row.id, isActive)
      }
      return setUserActive(row.id, isActive)
    },
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: ['admin-users'] })
      void queryClient.invalidateQueries({ queryKey: ['admin-platform-admins'] })
      void queryClient.invalidateQueries({ queryKey: ['admin-companies'] })
      toast.success(updated.is_active ? `Activated ${updated.display_name || updated.email}` : `Deactivated ${updated.display_name || updated.email}`)
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Could not update user')
    },
  })

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
      <div>
        <h1 className="font-display text-2xl font-semibold tracking-tight">Users</h1>
        <p className="mt-2 text-muted-foreground">
          Activate or deactivate people and manage their departments. Platform admins are listed separately.
        </p>
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        {platformAdmin ? (
          <label htmlFor={`${filterId}-company`} className="grid w-auto gap-1.5 text-sm">
            <span className="text-muted-foreground">Company</span>
            <Select
              id={`${filterId}-company`}
              fit
              value={companyId}
              onChange={(event) => updateListState({ company: event.target.value })}
            >
              {(companies.data ?? []).map((company) => (
                <option key={company.id} value={company.id}>
                  {company.name}
                </option>
              ))}
            </Select>
          </label>
        ) : null}
        <label htmlFor={`${filterId}-search`} className="grid min-w-0 flex-1 gap-1.5 text-sm">
          <span className="text-muted-foreground">Search</span>
          <span className="relative">
            <Search
              className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              id={`${filterId}-search`}
              type="search"
              value={search}
              onChange={(event) => updateListState({ q: event.target.value })}
              placeholder="Name or email"
              className="pl-10"
            />
          </span>
        </label>
      </div>

      {companies.error ? (
        <p className="text-sm text-destructive">
          {companies.error instanceof ApiError ? companies.error.message : 'Failed to load companies'}
        </p>
      ) : null}

      {platformAdmin && !companies.isLoading && !companies.error && (companies.data?.length ?? 0) === 0 ? (
        <p className="text-sm text-muted-foreground">No companies yet.</p>
      ) : null}

      {platformAdmin ? (
        <div className="flex flex-col gap-4">
          <div>
            <h2 className="font-display text-lg font-semibold tracking-tight">Platform admins</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Not members of the selected company. Genesis can activate additional platform admins.
            </p>
          </div>
          {platformAdmins.isLoading ? (
            <p className="text-sm text-muted-foreground">Loading platform admins…</p>
          ) : null}
          {platformAdmins.error ? (
            <p className="text-sm text-destructive">
              {platformAdmins.error instanceof ApiError
                ? platformAdmins.error.message
                : 'Failed to load platform admins'}
            </p>
          ) : null}
          {!platformAdmins.isLoading && !platformAdmins.error && visiblePlatform.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {search.trim() ? `No platform admins match “${search.trim()}”.` : 'No platform admins.'}
            </p>
          ) : null}
          <div className="grid gap-4">
            {visiblePlatform.map((row) => (
              <UserCard
                key={row.id}
                row={row}
                canToggle={canToggle(row)}
                pending={toggle.isPending}
                onToggle={() => toggle.mutate({ row, isActive: !row.is_active })}
              />
            ))}
          </div>
        </div>
      ) : null}

      <div className="flex flex-col gap-4">
        {platformAdmin ? (
          <div>
            <h2 className="font-display text-lg font-semibold tracking-tight">Company users</h2>
            <p className="mt-1 text-sm text-muted-foreground">People in the selected company.</p>
          </div>
        ) : null}
        {users.isLoading ? <p className="text-sm text-muted-foreground">Loading users…</p> : null}
        {users.error ? (
          <p className="text-sm text-destructive">
            {users.error instanceof ApiError ? users.error.message : 'Failed to load users'}
          </p>
        ) : null}

        {!users.isLoading && !users.error && tenantId && visibleCompany.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {search.trim() ? `No users match “${search.trim()}”.` : 'No users in this company.'}
          </p>
        ) : null}

        {tenantId ? (
          <TenantUsers
            key={tenantId}
            tenantId={tenantId}
            rows={visibleCompany}
            canToggle={canToggle}
            pending={toggle.isPending}
            onToggle={(row) => toggle.mutate({ row, isActive: !row.is_active })}
          />
        ) : null}
      </div>
    </div>
  )
}

function filterUsers(rows: AdminUser[], search: string): AdminUser[] {
  const needle = search.trim().toLowerCase()
  if (!needle) return rows
  return rows.filter((row) => {
    const haystack = `${row.display_name ?? ''} ${row.email ?? ''} ${row.username ?? ''}`.toLowerCase()
    return haystack.includes(needle)
  })
}
