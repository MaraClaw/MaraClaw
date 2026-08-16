import { useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { listOrgDepartments, listOrgMembers, listOrgUsers } from '@/lib/directory-api'
import { ApiError } from '@/lib/http'
import { cn } from '@/lib/utils'

const tabs = ['People', 'Synced', 'Departments'] as const

export function DirectoryPage() {
  const [tab, setTab] = useState<(typeof tabs)[number]>('People')
  const [search, setSearch] = useState('')
  const users = useQuery({ queryKey: ['directory', 'users'], queryFn: listOrgUsers })
  const members = useQuery({ queryKey: ['directory', 'members', search], queryFn: () => listOrgMembers(search) })
  const departments = useQuery({ queryKey: ['directory', 'departments'], queryFn: listOrgDepartments })

  const filteredUsers = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (users.data ?? []).filter((user) => {
      if (!q) return true
      return [user.display_name, user.email, user.username, user.title, user.role]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(q))
    })
  }, [users.data, search])

  const query = search.trim()

  return (
    <div className="space-y-5 p-6">
      <div>
        <h1 className="font-display text-2xl font-semibold">Directory</h1>
        <p className="text-sm text-muted-foreground">People in this company and members synced from IM providers.</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {tabs.map((item) => (
          <button
            key={item}
            type="button"
            className={cn(
              'rounded-lg px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted',
              tab === item && 'bg-muted text-foreground',
            )}
            onClick={() => setTab(item)}
          >
            {item}
          </button>
        ))}
        <Input
          type="search"
          className="ml-auto h-9 max-w-xs"
          placeholder="Search"
          aria-label="Search directory"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>

      {tab === 'People' ? (
        <PersonCardList
          loading={users.isLoading}
          error={users.error}
          count={filteredUsers.length}
          empty={query ? `No people match “${query}”.` : 'No people in this company yet.'}
          errorFallback="Unable to load people"
        >
          {filteredUsers.map((user) => {
            const name = user.display_name || user.email || user.username || 'Member'
            return (
              <PersonCard
                key={user.id}
                name={name}
                avatarUrl={user.avatar_url}
                description={contactLine(user.email, user.username)}
                badge={roleLabel(user.role)}
                detail={user.title}
              />
            )
          })}
        </PersonCardList>
      ) : null}

      {tab === 'Synced' ? (
        <PersonCardList
          loading={members.isLoading}
          error={members.error}
          count={(members.data ?? []).length}
          empty={
            query
              ? `No synced members match “${query}”.`
              : 'No synced org members. Connect Feishu, WeCom, or DingTalk first.'
          }
          errorFallback="Unable to load synced members"
        >
          {(members.data ?? []).map((member) => (
            <PersonCard
              key={member.id}
              name={member.name}
              avatarUrl={member.avatar_url}
              description={member.email}
              badge={member.provider_name ?? member.provider_type ?? 'Synced'}
              detail={joinDetail(member.title, member.department_path)}
            />
          ))}
        </PersonCardList>
      ) : null}

      {tab === 'Departments' ? (
        <div className="space-y-2">
          {departments.isLoading ? (
            <Loader2 className="size-4 animate-spin text-muted-foreground" />
          ) : null}
          <p className="text-xs text-muted-foreground">{departments.data?.total_member ?? 0} synced members</p>
          <ul className="space-y-2">
            {(departments.data?.items ?? []).map((dept) => (
              <li key={dept.id} className="flex items-center justify-between rounded-xl border border-border px-3 py-2">
                <div>
                  <p className="text-sm font-medium">{dept.name}</p>
                  <p className="text-xs text-muted-foreground">{dept.path}</p>
                </div>
                <Badge variant="soft">{dept.member_count ?? 0}</Badge>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  )
}

function PersonCardList({
  loading,
  error,
  count,
  empty,
  errorFallback,
  children,
}: {
  loading: boolean
  error: unknown
  count: number
  empty: string
  errorFallback: string
  children: ReactNode
}) {
  if (loading) {
    return (
      <div className="flex h-24 items-center justify-center text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
      </div>
    )
  }
  if (error) {
    return (
      <p className="text-sm text-destructive">
        {error instanceof ApiError ? error.message : errorFallback}
      </p>
    )
  }
  if (count === 0) {
    return <p className="text-sm text-muted-foreground">{empty}</p>
  }
  return <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">{children}</div>
}

function PersonCard({
  name,
  avatarUrl,
  description,
  badge,
  detail,
}: {
  name: string
  avatarUrl?: string | null
  description?: string | null
  badge: string
  detail?: string | null
}) {
  return (
    <Card className="h-full min-w-0 transition-colors hover:bg-muted/40">
      <CardHeader className="flex flex-row items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle className="flex min-w-0 items-center gap-2.5">
            <PersonAvatar name={name} src={avatarUrl} />
            <span className="min-w-0 wrap-break-word">{name}</span>
          </CardTitle>
          {description ? (
            <CardDescription className="wrap-break-word">{description}</CardDescription>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Badge variant="secondary">{badge}</Badge>
        </div>
      </CardHeader>
      {detail ? (
        <CardContent className="flex flex-wrap items-center gap-3">
          <span className="text-sm text-muted-foreground">{detail}</span>
        </CardContent>
      ) : null}
    </Card>
  )
}

function PersonAvatar({ name, src }: { name: string; src?: string | null }) {
  const [failed, setFailed] = useState(false)
  if (src && !failed) {
    return (
      <img
        src={src}
        alt=""
        width={28}
        height={28}
        aria-hidden
        draggable={false}
        onError={() => setFailed(true)}
        className="size-7 shrink-0 rounded-full object-cover outline outline-1 -outline-offset-1 outline-black/10 dark:outline-white/10"
      />
    )
  }
  return (
    <span
      aria-hidden
      className="inline-flex size-7 shrink-0 items-center justify-center rounded-full bg-muted text-[0.65rem] font-medium text-muted-foreground"
    >
      {initialsFromName(name)}
    </span>
  )
}

function roleLabel(role: string): string {
  if (role === 'org_admin') return 'Org admin'
  if (role === 'platform_admin') return 'Platform admin'
  if (role === 'agent_admin') return 'Agent admin'
  if (role === 'member') return 'Member'
  return role
}

function contactLine(email?: string | null, username?: string | null): string | undefined {
  const parts = [email, username].filter((value, index, all): value is string => {
    if (!value) return false
    return all.indexOf(value) === index
  })
  return parts.length ? parts.join(' · ') : undefined
}

function joinDetail(...values: Array<string | null | undefined>): string | undefined {
  const parts = values.filter((value): value is string => Boolean(value))
  const unique = parts.filter((value, index) => parts.indexOf(value) === index)
  return unique.length ? unique.join(' · ') : undefined
}

function initialsFromName(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '?'
  if (parts.length === 1) {
    const [first] = parts
    return first.slice(0, 2).toUpperCase()
  }
  const [first, last] = [parts[0], parts[parts.length - 1]]
  return `${first[0] ?? ''}${last[0] ?? ''}`.toUpperCase()
}
