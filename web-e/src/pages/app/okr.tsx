import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/hooks/use-auth'
import { useMutation, useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { OkrReports } from '@/components/okr-reports'
import { OkrObjectives } from './okr-objectives'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  getOkrSettings,
  listDailyReports,
  listOkrPeriods,
  submitDailyReport,
  updateOkrSettings,
} from '@/lib/okr-api'

function todayIso() {
  return new Date().toISOString().slice(0, 10)
}

export function OkrPage() {
  const scope = useWorkspaceSession()
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const isAdmin = user?.role === 'org_admin'
  const settings = useQuery({ queryKey: ['okr', 'settings'], queryFn: getOkrSettings })
  const periods = useQuery({ queryKey: ['okr', 'periods'], queryFn: listOkrPeriods })
  const current = periods.data?.find((period) => period.is_current) ?? periods.data?.[0]
  const [periodStart, setPeriodStart] = useState('')
  const selected = periods.data?.find((period) => period.start === periodStart) ?? current

  const reports = useQuery({ queryKey: ['okr', 'daily', todayIso()], queryFn: () => listDailyReports(todayIso()) })

  const [report, setReport] = useState('')

  function fail(error: unknown, fallback: string) {
    if (!scope.isCurrent()) return
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

      <label htmlFor="okr-period" className="block text-sm">
        <span className="text-muted-foreground">Period</span>
        <Select
          id="okr-period"
          fit
          className="mt-1"
          value={selected?.start ?? ''}
          onChange={scope.effect((event) => setPeriodStart(event.target.value))}
        >
          {(periods.data ?? []).map((period) => (
            <option key={period.start} value={period.start}>
              {period.label}
              {period.is_current ? ' (current)' : ''}
            </option>
          ))}
        </Select>
      </label>

      {selected ? <OkrObjectives key={`${selected.start}:${selected.end}`} period={selected} isAdmin={isAdmin} /> : null}

      <section className="space-y-3">
        <h2 className="font-display text-lg font-semibold">Today’s reports</h2>
        <Textarea value={report} onChange={scope.effect((event) => setReport(event.target.value))} placeholder="What did you finish today?" />
        <Button
          size="sm"
          disabled={!report.trim()}
          onClick={() => scope.isCurrent() &&
            void submitDailyReport({ report_date: todayIso(), content: report.trim() })
              .then(() => {
                if (!scope.isCurrent()) return
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

      <OkrReports isAdmin={isAdmin} enabled={Boolean(settings.data?.enabled)} />
    </div>
  )
}
