import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiError } from '@/lib/http'
import { agentStatusLabel, getUserDetail, roleLabel } from '@/lib/users-api'

export function UserDetailPage() {
  const { userId } = useParams<{ userId: string }>()
  const navigate = useNavigate()
  const location = useLocation()

  function goBack() {
    if (location.key !== 'default') {
      navigate(-1)
      return
    }
    navigate('/users')
  }

  const detail = useQuery({
    queryKey: ['admin-user', userId],
    queryFn: () => getUserDetail(userId!),
    enabled: Boolean(userId),
  })

  const person = detail.data

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <Button type="button" variant="outline" size="sm" className="w-fit" onClick={goBack}>
        <ArrowLeft className="size-3.5" aria-hidden />
        Back
      </Button>

      {detail.isLoading ? <p className="text-sm text-muted-foreground">Loading user…</p> : null}
      {detail.error ? (
        <p className="text-sm text-destructive">
          {detail.error instanceof ApiError ? detail.error.message : 'Failed to load user'}
        </p>
      ) : null}

      {person ? (
        <>
          <div>
            <h1 className="font-display text-2xl font-semibold tracking-tight">
              {person.display_name || person.email || 'User'}
            </h1>
            <p className="mt-2 text-muted-foreground">{person.email || person.username}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Badge variant="secondary">{roleLabel(person.role)}</Badge>
              {person.is_genesis ? <Badge variant="soft">Genesis</Badge> : null}
              <Badge variant={person.is_active ? 'success' : 'destructive'}>
                {person.is_active ? 'Active' : 'Inactive'}
              </Badge>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <UserStat label="Agents" value={person.agents_count} />
            <UserStat label="Messages used" value={person.quota_messages_used} />
            <UserStat label="Agent quota" value={person.quota_max_agents} />
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
              <CardDescription>Account fields returned by the admin users API.</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 text-sm sm:grid-cols-2">
              <Detail label="Username" value={person.username} />
              <Detail label="Source" value={person.source} />
              <Detail
                label="Created"
                value={person.created_at ? new Date(person.created_at).toLocaleString() : null}
              />
              <Detail label="Message period" value={person.quota_message_period} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Agents</CardTitle>
              <CardDescription>Digital employees this person created.</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              {person.agents.length === 0 ? (
                <p className="text-sm text-muted-foreground">No agents yet.</p>
              ) : (
                person.agents.map((agent) => (
                  <div
                    key={agent.id}
                    className="flex flex-wrap items-start justify-between gap-3 border-b border-border py-3 last:border-0 last:pb-0 first:pt-0"
                  >
                    <div>
                      <p className="font-medium">{agent.name}</p>
                      {agent.role_description ? (
                        <p className="mt-0.5 text-sm text-muted-foreground">{agent.role_description}</p>
                      ) : null}
                    </div>
                    <Badge
                      variant={
                        agent.is_expired || agent.status === 'error'
                          ? 'destructive'
                          : agent.status === 'running'
                            ? 'success'
                            : 'secondary'
                      }
                    >
                      {agentStatusLabel(agent.status, agent.is_expired)}
                    </Badge>
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </>
      ) : null}
    </div>
  )
}

function UserStat({ label, value }: { label: string; value: number | undefined }) {
  return (
    <div className="rounded-xl border border-border bg-card px-3.5 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 font-display text-xl font-semibold tabular-nums">
        {value == null ? '—' : value.toLocaleString()}
      </p>
    </div>
  )
}

function Detail({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-0.5 font-medium">{value || '—'}</p>
    </div>
  )
}
