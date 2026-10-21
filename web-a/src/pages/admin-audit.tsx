import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { adminAuditOptions } from '@/hooks/admin-audit-options'
import { useAuth } from '@/hooks/use-auth'
import { ADMIN_AUDIT_DEFAULT_LIMIT, type AdminAuditScope } from '@/lib/admin-audit-api'
import { ApiError } from '@/lib/http'
import { isPlatformAdminUser } from '@/lib/types/auth'

const filterId = z.string().trim().pipe(z.union([z.literal(''), z.uuid('Enter a valid UUID or leave this blank.')]))
const filterSchema = z.object({ action: z.string().trim(), tenant_id: filterId, actor_id: filterId })
type Filters = z.infer<typeof filterSchema>
const emptyFilters: Filters = { action: '', tenant_id: '', actor_id: '' }
const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
const buttonClass = 'h-auto min-h-11 max-w-full whitespace-normal'

export function AdminAuditPage() {
  const id = useId()
  const { user } = useAuth()
  const platformAdmin = isPlatformAdminUser(user)
  const [filters, setFilters] = useState(emptyFilters)
  const form = useForm<Filters>({ resolver: zodResolver(filterSchema), defaultValues: emptyFilters })
  const scope: AdminAuditScope = platformAdmin
    ? { role: 'platform_admin', ...filters }
    : { role: 'org_admin', action: filters.action }
  const activity = useQuery(adminAuditOptions(scope))
  const hasFilters = Object.values(filters).some(Boolean)

  function clearFilters() {
    form.reset(emptyFilters)
    setFilters(emptyFilters)
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 [overflow-wrap:anywhere]">
      <header className="flex flex-col gap-2">
        <h1 className="font-display text-2xl font-semibold tracking-tight">Recent activity</h1>
        <p className="text-muted-foreground">{platformAdmin ? 'Recent administrator actions across the platform.' : 'Recent administrator actions in your organization.'}</p>
        <p className="text-sm text-muted-foreground">Best-effort operational history, not a complete compliance record. Shows up to {ADMIN_AUDIT_DEFAULT_LIMIT} recent matching events, newest first.</p>
      </header>

      <Card>
        <CardHeader><CardTitle>Filter activity</CardTitle></CardHeader>
        <CardContent>
          <form noValidate onSubmit={form.handleSubmit(setFilters)} className="flex flex-col gap-4">
            <div className={`grid gap-4 ${platformAdmin ? 'sm:grid-cols-3' : ''}`}>
              <div className="flex min-w-0 flex-col gap-2">
                <Label htmlFor={`${id}-action`}>Action</Label>
                <Input id={`${id}-action`} placeholder="Exact action, e.g. company.create" {...form.register('action')} />
              </div>
              {platformAdmin ? (['tenant_id', 'actor_id'] as const).map((field) => (
                <div key={field} className="flex min-w-0 flex-col gap-2">
                  <Label htmlFor={`${id}-${field}`}>{field === 'tenant_id' ? 'Tenant ID' : 'Actor ID'}</Label>
                  <Input id={`${id}-${field}`} placeholder="UUID (optional)" aria-invalid={Boolean(form.formState.errors[field])} aria-describedby={form.formState.errors[field] ? `${id}-${field}-error` : undefined} {...form.register(field)} />
                  {form.formState.errors[field] ? <p id={`${id}-${field}-error`} role="alert" className="text-sm text-destructive">{form.formState.errors[field]?.message}</p> : null}
                </div>
              )) : null}
            </div>
            <div className="flex flex-wrap gap-3">
              <Button type="submit" className={buttonClass}>Apply filters</Button>
              <Button type="button" variant="outline" className={buttonClass} onClick={clearFilters}>Clear filters</Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <section aria-labelledby={`${id}-results`} aria-busy={activity.isFetching} className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id={`${id}-results`} className="font-display text-lg font-semibold">{activity.isSuccess ? `${activity.data.length} recent ${activity.data.length === 1 ? 'event' : 'events'}` : 'Activity results'}</h2>
          <Button type="button" variant="outline" className={buttonClass} disabled={activity.isFetching} onClick={() => void activity.refetch()}>Refresh activity</Button>
        </div>
        {activity.isPending ? <Card><CardContent role="status">{activity.isPaused ? 'Waiting for a connection. Activity will load when you are back online.' : 'Loading recent activity...'}</CardContent></Card> : activity.isError ? (
          <Card><CardContent role="alert" className="flex flex-col gap-3">
            <p className="font-medium">Could not load recent activity.</p>
            <p className="text-sm text-muted-foreground">{activity.error instanceof ApiError ? activity.error.message : 'Check your connection and try again. If this continues, contact your administrator.'}</p>
            <Button type="button" variant="outline" className={`${buttonClass} self-start`} onClick={() => void activity.refetch()}>Try again</Button>
          </CardContent></Card>
        ) : activity.data.length === 0 ? (
          <Card><CardContent className="flex flex-col gap-3">
            <p role="status">No recent activity{hasFilters ? ' matches these filters' : ' is available'}.</p>
            <p className="text-sm text-muted-foreground">{hasFilters ? 'Clear the filters to see other recent events.' : 'Refresh after an administrator makes a change. Some actions may not be recorded.'}</p>
          </CardContent></Card>
        ) : (
          <ol className="flex min-w-0 flex-col gap-3">
            {activity.data.map((event) => (
              <li key={event.id}>
                <Card className="gap-4 py-4">
                  <CardContent className="flex min-w-0 flex-col gap-3">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <h3 className="min-w-0 font-semibold">{event.action}</h3>
                      <p className="text-sm text-muted-foreground">{event.created_at ? <time dateTime={event.created_at}>{timeFormat.format(new Date(event.created_at))}</time> : 'Time unavailable'}</p>
                    </div>
                    <p className="text-sm text-muted-foreground">{event.actor_email ?? 'Actor unavailable'} <span>({event.actor_role})</span></p>
                    <p className="text-sm">Target: {event.target_type}{event.target_id ? ` / ${event.target_id}` : ' / ID unavailable'}</p>
                    <details className="min-w-0 border-t border-border pt-1">
                      <summary className="min-h-11 cursor-pointer content-center rounded-md py-2 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">View event details</summary>
                      <dl className="grid min-w-0 gap-3 py-3 text-sm sm:grid-cols-2">
                        {[
                          ['Event ID', event.id], ['Actor ID', event.actor_id ?? 'Unavailable'],
                          ['Tenant ID', event.tenant_id ?? 'Unavailable'], ['IP address', event.ip_address ?? 'Unavailable'],
                        ].map(([label, value]) => <div key={label}><dt className="text-muted-foreground">{label}</dt><dd>{value}</dd></div>)}
                      </dl>
                      {([['Changes', event.changes], ['Details', event.details]] as const).map(([label, value]) => (
                        <div key={label} className="flex min-w-0 flex-col gap-2 pb-3">
                          <h4 className="text-sm font-medium">{label}</h4>
                          <pre className="whitespace-pre-wrap rounded-lg bg-muted p-3 font-mono text-sm [overflow-wrap:anywhere]">{JSON.stringify(value, null, 2)}</pre>
                        </div>
                      ))}
                    </details>
                  </CardContent>
                </Card>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  )
}
