import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { adminAuditPageOptions } from '@/hooks/admin-audit-options'
import { useAuth } from '@/hooks/use-auth'
import { ADMIN_AUDIT_DEFAULT_LIMIT, type AdminAuditPageScope } from '@/lib/admin-audit-api'
import { adminAuditWindowSchema } from '@/lib/admin-audit-window'
import { ApiError } from '@/lib/http'
import { isPlatformAdminUser } from '@/lib/types/auth'

const filterId = z.string().trim().pipe(z.union([z.literal(''), z.uuid('Enter a valid UUID or leave this blank.')]))
const filterSchema = adminAuditWindowSchema.safeExtend({ action: z.string().trim(), tenant_id: filterId, actor_id: filterId })
type Filters = z.infer<typeof filterSchema>
const emptyFilters = { action: '', tenant_id: '', actor_id: '', start_at: '', end_at: '' }
const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
const buttonClass = 'h-auto min-h-11 max-w-full whitespace-normal'
const pageButtonClass = `${buttonClass} aria-disabled:pointer-events-none aria-disabled:opacity-50`

export function AdminAuditPage() {
  const id = useId()
  const { user } = useAuth()
  const platformAdmin = isPlatformAdminUser(user)
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [cursors, setCursors] = useState<readonly string[]>([])
  const [refresh, setRefresh] = useState(0)
  const form = useForm<z.input<typeof filterSchema>, unknown, Filters>({ resolver: zodResolver(filterSchema), defaultValues: emptyFilters })
  const scope: AdminAuditPageScope = platformAdmin
    ? { role: 'platform_admin', ...filters }
    : { role: 'org_admin', action: filters.action, start_at: filters.start_at, end_at: filters.end_at }
  const activity = useQuery(adminAuditPageOptions({ ...scope, cursor: cursors.at(-1) }, refresh))
  const hasFilters = Object.values(filters).some(Boolean)

  function applyFilters(next: Filters) {
    setFilters(next)
    setCursors([])
  }

  function clearFilters() {
    form.reset(emptyFilters)
    applyFilters(emptyFilters)
  }

  function refreshActivity() {
    setCursors([])
    setRefresh((value) => value + 1)
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 [overflow-wrap:anywhere]">
      <header className="flex flex-col gap-2">
        <h1 className="font-display text-2xl font-semibold tracking-tight">Recent activity</h1>
        <p className="text-muted-foreground">{platformAdmin ? 'Recent administrator actions across the platform.' : 'Recent administrator actions in your organization.'}</p>
        <p className="text-sm text-muted-foreground">Best-effort operational history, not a complete compliance record. Shows up to {ADMIN_AUDIT_DEFAULT_LIMIT} matching events per page, newest first. Browsing is not a snapshot: refresh to see new events or late commits.</p>
      </header>

      <Card>
        <CardHeader><CardTitle>Filter activity</CardTitle></CardHeader>
        <CardContent>
          <form noValidate onSubmit={form.handleSubmit(applyFilters)} className="flex flex-col gap-4">
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
            <fieldset className="flex min-w-0 flex-col gap-3">
              <legend className="mb-2 text-sm font-medium">Time window (optional)</legend>
              <p id={`${id}-window-help`} className="text-sm text-muted-foreground">Use RFC 3339 with an offset, such as 2026-10-21T12:30:00Z or 2026-10-21T20:30:00+08:00. Start is inclusive; end is exclusive. Leave either blank for an open bound.</p>
              <div className="grid gap-4 sm:grid-cols-2">
                {(['start_at', 'end_at'] as const).map((field) => (
                  <div key={field} className="flex min-w-0 flex-col gap-2">
                    <Label htmlFor={`${id}-${field}`}>{field === 'start_at' ? 'Start time' : 'End time'}</Label>
                    <Input id={`${id}-${field}`} type="text" autoComplete="off" spellCheck={false} placeholder="2026-10-21T12:30:00Z" aria-invalid={Boolean(form.formState.errors[field])} aria-describedby={`${id}-window-help${form.formState.errors[field] ? ` ${id}-${field}-error` : ''}`} {...form.register(field)} />
                    {form.formState.errors[field] ? <p id={`${id}-${field}-error`} role="alert" className="text-sm text-destructive">{form.formState.errors[field]?.message}</p> : null}
                  </div>
                ))}
              </div>
            </fieldset>
            <div className="flex flex-wrap gap-3">
              <Button type="submit" className={buttonClass}>Apply filters</Button>
              <Button type="button" variant="outline" className={buttonClass} onClick={clearFilters}>Clear filters</Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <section aria-labelledby={`${id}-results`} aria-busy={activity.isFetching} className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id={`${id}-results`} className="font-display text-lg font-semibold">{activity.isSuccess ? `${activity.data.items.length} recent ${activity.data.items.length === 1 ? 'event' : 'events'}` : 'Activity results'}</h2>
          <Button type="button" variant="outline" className={buttonClass} onClick={refreshActivity}>Refresh activity</Button>
        </div>
        <p role="status" className="text-sm text-muted-foreground">Page {cursors.length + 1}{activity.isSuccess && activity.data.next_cursor === null ? ' · End of available history' : ''}</p>
        {filters.start_at || filters.end_at ? <p className="text-sm text-muted-foreground">Applied UTC window: {filters.start_at ?? 'Any start'} (inclusive) to {filters.end_at ?? 'Any end'} (exclusive).</p> : null}
        {activity.isPending ? <Card><CardContent role="status">{activity.isPaused ? 'Waiting for a connection. Activity will load when you are back online.' : 'Loading recent activity...'}</CardContent></Card> : activity.isError ? (
          <Card><CardContent role="alert" className="flex flex-col gap-3">
            <p className="font-medium">Could not load recent activity.</p>
            <p className="text-sm text-muted-foreground">{activity.error instanceof ApiError ? activity.error.message : 'Check your connection and try again. If this continues, contact your administrator.'}</p>
            {cursors.length > 0 ? <p className="text-sm text-muted-foreground">If this cursor is no longer valid, refresh activity to restart from the newest events.</p> : null}
            <Button type="button" variant="outline" className={`${buttonClass} self-start`} onClick={() => void activity.refetch()}>Try again</Button>
          </CardContent></Card>
        ) : activity.data.items.length === 0 ? (
          <Card><CardContent className="flex flex-col gap-3">
            <p role="status">{cursors.length > 0 ? 'No older activity is available on this page.' : `No recent activity${hasFilters ? ' matches these filters' : ' is available'}.`}</p>
            <p className="text-sm text-muted-foreground">{cursors.length > 0 ? 'Go back to the previous page or refresh to restart from the newest events.' : hasFilters ? 'Clear the filters to see other recent events.' : 'Refresh after an administrator makes a change. Some actions may not be recorded.'}</p>
          </CardContent></Card>
        ) : (
          <ol className="flex min-w-0 flex-col gap-3">
            {activity.data.items.map((event) => (
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
                           ['Timestamp', event.created_at ?? 'Unavailable'],
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
        <nav aria-label="Activity pages" className="flex flex-wrap gap-3">
          {/* Keep inactive paging controls focusable so keyboard focus survives page changes. */}
          <Button type="button" variant="outline" className={pageButtonClass} aria-disabled={cursors.length === 0} onClick={() => {
            if (cursors.length > 0) setCursors((previous) => previous.slice(0, -1))
          }}>Back</Button>
          <Button type="button" variant="outline" className={pageButtonClass} aria-disabled={!activity.isSuccess || activity.isFetching || activity.data.next_cursor === null} onClick={() => {
            const next = activity.data?.next_cursor
            if (activity.isSuccess && !activity.isFetching && next !== undefined && next !== null) setCursors((previous) => [...previous, next])
          }}>Older</Button>
        </nav>
      </section>
    </div>
  )
}
