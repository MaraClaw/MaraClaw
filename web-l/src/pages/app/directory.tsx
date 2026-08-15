import { useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useMemo, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { listOrgDepartments, listOrgMembers, listOrgUsers } from '@/lib/directory-api'
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
          className="ml-auto h-9 max-w-xs"
          placeholder="Search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>

      {tab === 'People' ? (
        <DirectoryTable
          loading={users.isLoading}
          empty="No people in this company yet."
          rows={filteredUsers.map((user) => [
            user.display_name,
            user.email ?? user.username ?? '—',
            user.title ?? '—',
            user.role,
          ])}
        />
      ) : null}

      {tab === 'Synced' ? (
        <DirectoryTable
          loading={members.isLoading}
          empty="No synced org members. Connect Feishu, WeCom, or DingTalk first."
          rows={(members.data ?? []).map((member) => [
            member.name,
            member.email ?? '—',
            member.title ?? member.department_path ?? '—',
            member.provider_name ?? member.provider_type ?? 'synced',
          ])}
        />
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

function DirectoryTable({
  loading,
  empty,
  rows,
}: {
  loading: boolean
  empty: string
  rows: string[][]
}) {
  if (loading) {
    return (
      <div className="flex h-24 items-center justify-center text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
      </div>
    )
  }
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">{empty}</p>
  }
  return (
    <div className="overflow-x-auto rounded-2xl border border-border">
      <table className="w-full text-left text-sm">
        <thead className="bg-muted/50 text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-2 font-medium">Name</th>
            <th className="px-3 py-2 font-medium">Contact</th>
            <th className="px-3 py-2 font-medium">Title</th>
            <th className="px-3 py-2 font-medium">Role</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row[0]}-${index}`} className="border-t border-border">
              {row.map((cell, cellIndex) => (
                <td key={`${index}-${cellIndex}`} className="px-3 py-2">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
