import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

import { SectionRail, type SectionRailItem } from '@/components/layout/section-rail'
import { PersonCard, PersonCardList } from '@/components/directory-people'
import { OrgDepartments } from '@/components/org-departments'
import { Input } from '@/components/ui/input'
import { listOrgUsers } from '@/lib/directory-api'

const tabs = [
  { id: 'people', label: 'People', icon: 'people' },
  { id: 'departments', label: 'Departments', icon: 'departments' },
] as const satisfies readonly SectionRailItem[]

type DirectoryTab = (typeof tabs)[number]['id']

function isDirectoryTab(value: string | null): value is DirectoryTab {
  return value === 'people' || value === 'departments'
}

export function DirectoryPage() {
  const scope = useWorkspaceSession()
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedTab = searchParams.get('tab')
  const tab: DirectoryTab = isDirectoryTab(requestedTab) ? requestedTab : 'people'
  const search = searchParams.get('q') ?? ''

  function setTab(next: DirectoryTab) {
    if (!scope.isCurrent()) return
    const copy = new URLSearchParams(searchParams)
    if (next === 'people') copy.delete('tab')
    else copy.set('tab', next)
    setSearchParams(copy, { replace: true })
  }

  function setSearch(value: string) {
    if (!scope.isCurrent()) return
    const copy = new URLSearchParams(searchParams)
    if (value) copy.set('q', value)
    else copy.delete('q')
    setSearchParams(copy, { replace: true })
  }
  const users = useQuery({ queryKey: ['directory', 'users'], queryFn: listOrgUsers })

  const filteredUsers = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (users.data ?? []).filter((user) => {
      if (!q) return true
      return [user.display_name, user.email, user.title, user.role]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(q))
    })
  }, [users.data, search])

  const query = search.trim()

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_auto_1fr] overflow-hidden md:grid-cols-[6rem_1fr] md:grid-rows-[auto_1fr]">
      <div className="min-w-0 bg-background px-5 py-4 md:col-start-2 md:row-start-1">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="font-display text-2xl font-semibold">Directory</h1>
            <p className="text-sm text-muted-foreground">
              People and org departments in this company.
            </p>
          </div>
          {tab !== 'departments' ? (
            <Input
              type="search"
              className="h-9 max-w-xs"
              placeholder="Search"
              aria-label="Search directory"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          ) : null}
        </div>
      </div>
      <SectionRail
        items={tabs}
        active={tab}
        onSelect={setTab}
        label="Directory sections"
        className="md:col-start-1 md:row-start-1 md:row-span-2"
      />
      <div className="min-h-0 space-y-5 overflow-y-auto overscroll-y-contain p-6 md:col-start-2 md:row-start-2">
      {tab === 'people' ? (
        <PersonCardList
          loading={users.isLoading}
          error={users.error}
          count={filteredUsers.length}
          empty={query ? `No people match “${query}”.` : 'No people in this company yet.'}
          errorFallback="Unable to load people"
        >
          {filteredUsers.map((user) => {
            const name = user.display_name || user.email || 'Member'
            return (
              <PersonCard
                key={user.id}
                name={name}
                avatarUrl={user.avatar_url}
                description={user.email}
                badge={roleLabel(user.role)}
                detail={user.title}
              />
            )
          })}
        </PersonCardList>
      ) : null}

      {tab === 'departments' ? <OrgDepartments /> : null}
        </div>
    </div>
  )
}

function roleLabel(role: string): string {
  if (role === 'org_admin') return 'Org admin'
  if (role === 'platform_admin') return 'Platform admin'
  if (role === 'agent_admin') return 'Agent admin'
  if (role === 'member') return 'Member'
  return role
}
