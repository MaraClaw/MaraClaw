import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ApiError } from '@/lib/http'
import { agentStatusLabel, getUserDetail, roleLabel } from '@/lib/users-api'
import { useTranslation } from '@/hooks/use-translation'

export function UserDetailPage() {
  const { t, owned, date } = useTranslation()
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
    queryFn: ({ signal }) => getUserDetail(userId ?? '', signal),
    enabled: Boolean(userId),
  })

  const person = detail.data

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <Button type="button" variant="outline" size="sm" className="w-fit" onClick={goBack}>
        <ArrowLeft className="size-3.5" aria-hidden />
        {t('Back')}
      </Button>

      {detail.isLoading ? <p className="text-sm text-muted-foreground">{t('Loading user…')}</p> : null}
      {detail.error ? (
        <ErrorToast error={detail.error} message={detail.error instanceof ApiError ? detail.error.message : t('Failed to load user')} />
      ) : null}

      {person ? (
        <>
          <div>
            <h1 className="font-display text-2xl font-semibold tracking-tight">
              {person.display_name || person.email || t('User')}
            </h1>
            <p className="mt-2 text-muted-foreground">{person.email || t('No email')}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Badge variant="secondary">{owned(roleLabel(person.role))}</Badge>
              {person.is_genesis ? <Badge variant="soft">{t('Genesis')}</Badge> : null}
              <Badge variant={person.is_active ? 'success' : 'destructive'}>
                {t(person.is_active ? 'Active' : 'Inactive')}
              </Badge>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <UserStat label={t('Agents')} value={person.agents_count} />
            <UserStat label={t('Messages used')} value={person.quota_messages_used} />
            <UserStat
              label={t('Agent quota')}
              value={person.effective_max_agents === undefined ? person.quota_max_agents : person.effective_max_agents}
              nullLabel={t('Unlimited')}
            />
          </div>

          <Card>
            <CardHeader>
              <CardTitle>{t('Details')}</CardTitle>
              <CardDescription>{t('Account fields returned by the admin users API.')}</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 text-sm sm:grid-cols-2">
              <Detail label={t('Source')} value={person.source} />
              <Detail
                label={t('Created')}
                value={person.created_at ? date(person.created_at) : null}
              />
              <Detail label={t('Message period')} value={person.quota_message_period} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('Agents')}</CardTitle>
              <CardDescription>{t('Digital employees this person created.')}</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              {person.agents.length === 0 ? (
                <p className="text-sm text-muted-foreground">{t('No agents yet.')}</p>
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
                      {owned(agentStatusLabel(agent.status, agent.is_expired))}
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

function UserStat({
  label,
  value,
  nullLabel = '—',
}: {
  label: string
  value: number | null | undefined
  nullLabel?: string
}) {
  const { number } = useTranslation()
  return (
    <div className="rounded-xl border border-border bg-card px-3.5 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 font-display text-xl font-semibold tabular-nums">
        {value === undefined ? '—' : value === null ? nullLabel : number(value)}
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
