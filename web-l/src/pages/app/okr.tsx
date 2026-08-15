import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/hooks/use-auth'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  createKeyResult,
  createObjective,
  getOkrSettings,
  listCompanyReports,
  listDailyReports,
  listObjectives,
  listOkrPeriods,
  membersWithoutOkr,
  submitDailyReport,
  syncOkrRelationships,
  triggerOkrOutreach,
  updateKrProgress,
  updateOkrSettings,
} from '@/lib/okr-api'

function todayIso() {
  return new Date().toISOString().slice(0, 10)
}

export function OkrPage() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const isAdmin = user?.role === 'org_admin'
  const settings = useQuery({ queryKey: ['okr', 'settings'], queryFn: getOkrSettings })
  const periods = useQuery({ queryKey: ['okr', 'periods'], queryFn: listOkrPeriods })
  const current = periods.data?.find((period) => period.is_current) ?? periods.data?.[0]
  const [periodStart, setPeriodStart] = useState('')
  const selected = periods.data?.find((period) => period.start === periodStart) ?? current

  useEffect(() => {
    if (!periodStart && current) setPeriodStart(current.start)
  }, [current, periodStart])

  const objectives = useQuery({
    queryKey: ['okr', 'objectives', selected?.start, selected?.end],
    queryFn: () => listObjectives(selected?.start, selected?.end),
    enabled: Boolean(selected),
  })
  const reports = useQuery({ queryKey: ['okr', 'daily', todayIso()], queryFn: () => listDailyReports(todayIso()) })
  const company = useQuery({ queryKey: ['okr', 'company'], queryFn: listCompanyReports })
  const gaps = useQuery({
    queryKey: ['okr', 'gaps'],
    queryFn: membersWithoutOkr,
    enabled: Boolean(settings.data?.enabled),
  })

  const [title, setTitle] = useState('')
  const [report, setReport] = useState('')

  function fail(error: unknown, fallback: string) {
    toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : fallback)
  }

  const saveSettings = useMutation({
    mutationFn: (enabled: boolean) => updateOkrSettings({ enabled }),
    onSuccess() {
      toast.success('OKR settings saved')
      void queryClient.invalidateQueries({ queryKey: ['okr'] })
    },
    onError(error) {
      fail(error, 'Only org admins can change OKR settings')
    },
  })

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold">OKR</h1>
          <p className="text-sm text-muted-foreground">
            Track company and personal objectives. Members update progress through the OKR agent; org admins can edit here.
          </p>
        </div>
        {settings.data?.okr_agent_id ? (
          <Button asChild variant="outline" size="sm">
            <Link to={`/app/agents/${settings.data.okr_agent_id}/chat`}>Open OKR agent</Link>
          </Button>
        ) : null}
      </div>

      <section className="rounded-2xl border border-border p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Badge variant="soft">{settings.data?.enabled ? 'Enabled' : 'Disabled'}</Badge>
          <span className="text-sm text-muted-foreground">
            {settings.data?.period_frequency ?? 'quarterly'}
            {settings.data?.daily_report_enabled ? ` · daily report ${settings.data.daily_report_time}` : ''}
          </span>
          {isAdmin ? (
            <Button size="sm" variant="outline" onClick={() => saveSettings.mutate(!settings.data?.enabled)}>
              {settings.data?.enabled ? 'Disable' : 'Enable'}
            </Button>
          ) : null}
        </div>
      </section>

      <label className="block max-w-xs text-sm">
        <span className="text-muted-foreground">Period</span>
        <select
          className="mt-1 h-10 w-full rounded-xl border border-input bg-transparent px-3 text-sm"
          value={periodStart}
          onChange={(event) => setPeriodStart(event.target.value)}
        >
          {(periods.data ?? []).map((period) => (
            <option key={period.start} value={period.start}>
              {period.label}
              {period.is_current ? ' (current)' : ''}
            </option>
          ))}
        </select>
      </label>

      {objectives.isLoading ? <Loader2 className="size-4 animate-spin text-muted-foreground" /> : null}

      <ul className="space-y-3">
        {(objectives.data ?? []).map((objective) => (
          <li key={objective.id} className="rounded-2xl border border-border p-4">
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="font-medium">{objective.title}</p>
                <p className="text-xs text-muted-foreground">
                  {objective.owner_name ?? objective.owner_type} · {objective.status}
                </p>
              </div>
              <Badge variant="soft">{objective.owner_type}</Badge>
            </div>
            {objective.description ? <p className="mt-2 text-sm text-muted-foreground">{objective.description}</p> : null}
            <ul className="mt-3 space-y-2">
              {objective.key_results.map((kr) => {
                const ratio = kr.target_value ? Math.min(1, kr.current_value / kr.target_value) : 0
                return (
                  <li key={kr.id}>
                    <div className="flex justify-between text-xs">
                      <span>{kr.title}</span>
                      <span>
                        {kr.current_value}/{kr.target_value}
                        {kr.unit ? ` ${kr.unit}` : ''}
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
                      <div className="h-full bg-primary" style={{ width: `${ratio * 100}%` }} />
                    </div>
                    {isAdmin ? (
                      <button
                        type="button"
                        className="mt-1 text-xs text-primary"
                        onClick={() => {
                          const next = window.prompt('New current value', String(kr.current_value))
                          if (!next) return
                          void updateKrProgress(kr.id, Number(next)).then(() =>
                            queryClient.invalidateQueries({ queryKey: ['okr', 'objectives'] }),
                          )
                        }}
                      >
                        Update progress
                      </button>
                    ) : null}
                  </li>
                )
              })}
            </ul>
            {isAdmin && selected ? (
              <AddKeyResult
                objectiveId={objective.id}
                onAdded={() => queryClient.invalidateQueries({ queryKey: ['okr', 'objectives'] })}
              />
            ) : null}
          </li>
        ))}
      </ul>

      {isAdmin && selected ? (
        <form
          className="space-y-2 rounded-2xl border border-border p-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (!title.trim()) return
            void createObjective({
              title: title.trim(),
              owner_type: 'company',
              period_start: selected.start,
              period_end: selected.end,
            })
              .then(() => {
                setTitle('')
                toast.success('Objective created')
                void queryClient.invalidateQueries({ queryKey: ['okr', 'objectives'] })
              })
              .catch((error) => fail(error, 'Could not create objective'))
          }}
        >
          <Label htmlFor="obj-title">New company objective</Label>
          <div className="flex gap-2">
            <Input id="obj-title" value={title} onChange={(event) => setTitle(event.target.value)} />
            <Button type="submit" size="sm">
              Add
            </Button>
          </div>
        </form>
      ) : null}

      <section className="space-y-3">
        <h2 className="font-display text-lg font-semibold">Today’s reports</h2>
        <Textarea value={report} onChange={(event) => setReport(event.target.value)} placeholder="What did you finish today?" />
        <Button
          size="sm"
          disabled={!report.trim()}
          onClick={() =>
            void submitDailyReport({ report_date: todayIso(), content: report.trim() })
              .then(() => {
                setReport('')
                toast.success('Report submitted')
                void queryClient.invalidateQueries({ queryKey: ['okr', 'daily'] })
              })
              .catch((error) => fail(error, 'Could not submit report'))
          }
        >
          Submit my report
        </Button>
        <ul className="space-y-2">
          {(reports.data ?? []).map((item) => (
            <li key={item.id} className="rounded-xl border border-border px-3 py-2 text-sm">
              <span className="font-medium">{item.display_name}</span>{' '}
              <span className="text-muted-foreground">{item.status}</span>
              {item.content ? <p className="mt-1 whitespace-pre-wrap">{item.content}</p> : null}
            </li>
          ))}
        </ul>
      </section>

      <section className="space-y-3">
        <h2 className="font-display text-lg font-semibold">Company reports</h2>
        {(company.data ?? []).map((item) => (
          <article key={item.id} className="rounded-xl border border-border p-3 text-sm">
            <p className="font-medium">
              {item.period_label} · {item.report_type}
            </p>
            <p className="text-xs text-muted-foreground">
              {item.submitted_count} submitted · {item.missing_count} missing
            </p>
            <p className="mt-2 line-clamp-6 whitespace-pre-wrap text-muted-foreground">{item.content}</p>
          </article>
        ))}
      </section>

      {isAdmin && settings.data?.enabled ? (
        <section className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                void syncOkrRelationships().then(() => toast.success('Relationships synced')).catch((error) => fail(error, 'Sync failed'))
              }
            >
              Sync relationships
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                void triggerOkrOutreach().then(() => toast.success('Outreach started')).catch((error) => fail(error, 'Outreach failed'))
              }
            >
              Nudge missing OKRs
            </Button>
          </div>
          <p className="text-sm text-muted-foreground">
            {gaps.data?.total ?? 0} tracked members without an OKR this period.
          </p>
          <ul className="space-y-1 text-sm">
            {(gaps.data?.members_without_okr ?? []).slice(0, 20).map((member) => (
              <li key={`${member.type}-${member.id}`}>
                {member.display_name} <span className="text-muted-foreground">{member.type}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}

function AddKeyResult({ objectiveId, onAdded }: { objectiveId: string; onAdded: () => void }) {
  const [title, setTitle] = useState('')
  return (
    <div className="mt-3 flex gap-2">
      <Input
        className="h-9"
        placeholder="Add a key result"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
      />
      <Button
        size="sm"
        variant="outline"
        onClick={() => {
          if (!title.trim()) return
          void createKeyResult(objectiveId, { title: title.trim(), target_value: 100 }).then(() => {
            setTitle('')
            onAdded()
          })
        }}
      >
        KR
      </Button>
    </div>
  )
}
