import { useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { ApiError, formatApiDetail } from '@/lib/http'
import { createKeyResult, listCompanyReports, membersWithoutOkr, syncOkrRelationships, triggerOkrOutreach } from '@/lib/okr-api'

export function OkrReports({ isAdmin, enabled }: { readonly isAdmin: boolean; readonly enabled: boolean }) {
  const scope = useWorkspaceSession()
  const company = useQuery({ queryKey: ['okr', 'company'], queryFn: listCompanyReports })
  const gaps = useQuery({ queryKey: ['okr', 'gaps'], queryFn: membersWithoutOkr, enabled })
  function fail(error: unknown, fallback: string) {
    if (!scope.isCurrent()) return
    toast.error(error instanceof ApiError ? (formatApiDetail(error.detail) ?? error.message) : fallback)
  }
  return (
    <>
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
      {isAdmin && enabled ? (
        <section className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => scope.isCurrent() &&
                void syncOkrRelationships().then(scope.effect(() => toast.success('Relationships synced'))).catch((error) => fail(error, 'Sync failed'))
              }
            >
              Sync relationships
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => scope.isCurrent() &&
                void triggerOkrOutreach().then(scope.effect(() => toast.success('Outreach started'))).catch((error) => fail(error, 'Outreach failed'))
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
    </>
  )
}

export function AddKeyResult({ objectiveId, onAdded }: { readonly objectiveId: string; readonly onAdded: () => void }) {
  const scope = useWorkspaceSession()
  const [title, setTitle] = useState('')
  return (
    <div className="mt-3 flex gap-2">
      <Input
        className="h-9"
        placeholder="Add a key result"
        value={title}
        onChange={scope.effect((event) => setTitle(event.target.value))}
      />
      <Button
        size="sm"
        variant="outline"
        onClick={() => {
          if (!scope.isCurrent() || !title.trim()) return
          void createKeyResult(objectiveId, { title: title.trim(), target_value: 100 }).then(scope.effect(() => {
            setTitle('')
            onAdded()
          })).catch(scope.rethrowCurrent)
        }}
      >
        KR
      </Button>
    </div>
  )
}
