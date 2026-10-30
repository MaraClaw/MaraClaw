import { useInfiniteQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { useSearchParams } from 'react-router-dom'

import { SectionRail, type SectionRailItem } from '@/components/layout/section-rail'
import { PersonCard, PersonCardList } from '@/components/directory-people'
import { OrgDepartments } from '@/components/org-departments'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { listOrgUsersPage } from '@/lib/directory-api'
import { userFacingRequestError } from '@/lib/http'

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
  const query = search.trim()
  const normalizedSearch = query.toLowerCase()
  const users = useInfiniteQuery<Awaited<ReturnType<typeof listOrgUsersPage>>, Error, string | null>({
    queryKey: ['directory', 'users', 'page', normalizedSearch],
    initialPageParam: null,
    queryFn: ({ pageParam, signal }) => listOrgUsersPage({
      search: normalizedSearch, cursor: pageParam, signal, owner: scope.owner,
    }),
    getNextPageParam: (page) => page.next_cursor,
    enabled: tab === 'people',
    retry: false,
  })
  const loadedUsers = users.data?.pages.flatMap((page) => page.items) ?? []

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
        <>
        {!users.isError || users.data ? (
        <PersonCardList
          loading={users.isLoading}
          error={null}
          count={loadedUsers.length}
          empty={query ? `No people match “${query}”.` : 'No people in this company yet.'}
          errorFallback="Unable to load people"
        >
          {loadedUsers.map((user) => {
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
        {users.isError ? (
          <div className="space-y-3">
            <p role="alert" className="text-sm text-destructive">
              {userFacingRequestError(users.error, users.isFetchNextPageError
                ? 'Unable to load more people.'
                : users.data ? 'Unable to refresh people.' : 'Unable to load people.')}
            </p>
            <Button
              variant="outline"
              className="min-h-11"
              disabled={users.isFetching}
              onClick={() => users.isFetchNextPageError ? users.fetchNextPage() : users.refetch()}
            >
              Retry
            </Button>
          </div>
        ) : null}
        {users.hasNextPage && !users.isFetchNextPageError ? (
          <Button
            variant="outline"
            className="min-h-11 min-w-32"
            disabled={users.isFetching}
            onClick={() => users.fetchNextPage()}
          >
            {users.isFetchingNextPage ? 'Loading more…' : 'Load more'}
          </Button>
        ) : null}
        <p role="status" className="sr-only">
          {users.isLoading ? 'Loading people.' : users.isFetchingNextPage ? 'Loading more people.' : `${loadedUsers.length} people loaded.`}
        </p>
        </>
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
