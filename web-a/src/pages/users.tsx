import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, Search } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { useAuth } from '@/hooks/use-auth'
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

function roleLabel(role: string): string {
  if (role === 'org_admin') return 'Org admin'
  if (role === 'platform_admin') return 'Platform admin'
  if (role === 'agent_admin') return 'Agent admin'
  return 'Member'
}

export function UsersPage() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const platformAdmin = isPlatformAdminUser(user)
  const genesis = isGenesisAdmin(user)
  const [search, setSearch] = useState('')
  const [companyId, setCompanyId] = useState('')

  const companies = useQuery({
    queryKey: ['admin-companies'],
    queryFn: () => listCompanies(),
    enabled: platformAdmin,
  })

  useEffect(() => {
    if (!platformAdmin || companyId || !companies.data?.length) return
    const own = companies.data.find((company) => company.id === user?.tenant_id)
    setCompanyId(own?.id ?? companies.data[0].id)
  }, [platformAdmin, companyId, companies.data, user?.tenant_id])

  const users = useQuery({
    queryKey: ['admin-users', platformAdmin ? companyId : user?.tenant_id],
    queryFn: () => listUsers(platformAdmin ? companyId || undefined : undefined),
    enabled: !platformAdmin || Boolean(companyId),
  })

  const platformAdmins = useQuery({
    queryKey: ['admin-platform-admins'],
    queryFn: listPlatformAdmins,
    enabled: platformAdmin,
  })

  const rows = useMemo(() => {
    const byId = new Map<string, AdminUser>()
    for (const row of users.data ?? []) byId.set(row.id, row)
    if (platformAdmin) {
      for (const admin of platformAdmins.data ?? []) {
        const existing = byId.get(admin.id)
        if (existing) {
          byId.set(admin.id, { ...existing, is_genesis: admin.is_genesis, role: 'platform_admin' })
        } else {
          byId.set(admin.id, asAdminUser(admin))
        }
      }
    }
    return [...byId.values()]
  }, [platformAdmin, platformAdmins.data, users.data])

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    if (!needle) return rows
    return rows.filter((row) => {
      const haystack = `${row.display_name ?? ''} ${row.email ?? ''} ${row.username ?? ''}`.toLowerCase()
      return haystack.includes(needle)
    })
  }, [search, rows])

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
          Activate or deactivate people in this company. Genesis admins can also manage additional admins.
        </p>
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        {platformAdmin ? (
          <label className="grid w-full gap-1.5 text-sm sm:w-64">
            <span className="text-muted-foreground">Company</span>
            <span className="relative">
              <select
                value={companyId}
                onChange={(event) => setCompanyId(event.target.value)}
                className="h-11 w-full appearance-none rounded-xl border border-input bg-card px-3.5 pe-10 text-sm text-foreground shadow-sm outline-none transition-[border-color,box-shadow] focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35"
              >
                {(companies.data ?? []).map((company) => (
                  <option key={company.id} value={company.id}>
                    {company.name}
                  </option>
                ))}
              </select>
              <ChevronDown
                className="pointer-events-none absolute top-1/2 right-3.5 size-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden
              />
            </span>
          </label>
        ) : null}
        <label className="grid min-w-0 flex-1 gap-1.5 text-sm">
          <span className="text-muted-foreground">Search</span>
          <span className="relative">
            <Search
              className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <Input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Name or email"
              className="pl-10"
            />
          </span>
        </label>
      </div>

      {users.isLoading || (platformAdmin && platformAdmins.isLoading) ? (
        <p className="text-sm text-muted-foreground">Loading users…</p>
      ) : null}
      {users.error ? (
        <p className="text-sm text-destructive">
          {users.error instanceof ApiError ? users.error.message : 'Failed to load users'}
        </p>
      ) : null}

      {!users.isLoading && visible.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {search.trim() ? `No users match “${search.trim()}”.` : 'No users in this company.'}
        </p>
      ) : null}

      <div className="grid gap-4">
        {visible.map((row) => (
            <Card key={row.id}>
              <CardHeader className="flex flex-row items-start justify-between gap-4">
                <div>
                  <CardTitle>{row.display_name || row.email || 'User'}</CardTitle>
                  <CardDescription>{row.email || row.username}</CardDescription>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="secondary">{roleLabel(row.role)}</Badge>
                  {row.is_genesis ? <Badge variant="soft">Genesis</Badge> : null}
                  <Badge variant={row.is_active ? 'success' : 'destructive'}>
                    {row.is_active ? 'Active' : 'Inactive'}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="flex flex-wrap items-center gap-3">
                <span className="text-sm text-muted-foreground">{row.agents_count} agents</span>
                {canToggle(row) ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={toggle.isPending}
                    onClick={() => toggle.mutate({ row, isActive: !row.is_active })}
                  >
                    {row.is_active ? 'Deactivate' : 'Activate'}
                  </Button>
                ) : null}
              </CardContent>
            </Card>
          ))}
      </div>
    </div>
  )
}
