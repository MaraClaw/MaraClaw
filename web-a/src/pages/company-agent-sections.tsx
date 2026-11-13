import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useLayoutEffect, useRef } from 'react'
import { toast } from 'sonner'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { companyAgentSectionsOptions } from '@/hooks/company-options'
import { useSessionMutation as useMutation } from '@/hooks/use-session-mutation'
import { useTranslation } from '@/hooks/use-translation'
import { updateCompanyAgentSections } from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import type { Translator } from '@/lib/i18n'
import type { PlainMessageKey } from '@/lib/translations'

const sectionLabels: ReadonlyMap<string, PlainMessageKey> = new Map([
  ['chat', 'Chat'],
  ['files', 'Files'],
  ['skills', 'Skills'],
  ['tools', 'Tools'],
  ['mcp', 'MCP'],
  ['tasks', 'Tasks'],
  ['schedules', 'Schedules'],
  ['activity', 'Activity'],
  ['channels', 'Channels'],
  ['people', 'People'],
  ['access', 'Access'],
  ['control', 'Control'],
  ['vault', 'Vault'],
  ['pages', 'Pages'],
  ['browser', 'Browser'],
  ['settings', 'Settings'],
])

function sectionLabel(t: Translator, id: string) {
  const key = sectionLabels.get(id)
  return key === undefined ? id : t(key)
}

type SectionToggle = {
  readonly id: string
  readonly visible: boolean
  readonly hidden: readonly string[]
}

export function CompanyAgentSections({ companyId }: { readonly companyId: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const options = companyAgentSectionsOptions(companyId)
  const sections = useQuery(options)
  const titleId = useId()
  const hintId = useId()
  const switchIdPrefix = useId()
  const restoreFocusTo = useRef<string | null>(null)

  const update = useMutation({
    mutationFn: ({ hidden }: SectionToggle) => updateCompanyAgentSections(companyId, hidden),
    onSuccess: (data, { id, visible }) => {
      queryClient.setQueryData(options.queryKey, data)
      toast.success(t(
        visible ? '{section} is now visible to members' : '{section} is now hidden from members',
        { section: sectionLabel(t, id) },
      ))
    },
  })

  // A disabled control can drop focus; hand it back to the switch that was just used.
  useLayoutEffect(() => {
    const id = restoreFocusTo.current
    if (update.isPending || id === null) return
    restoreFocusTo.current = null
    if (document.activeElement && document.activeElement !== document.body) return
    document.getElementById(`${switchIdPrefix}-${id}`)?.focus()
  }, [update.isPending, switchIdPrefix])

  const data = sections.data
  const hidden = new Set(update.isPending ? update.variables.hidden : data?.hidden_sections)
  const visibleCount = data ? data.sections.filter((id) => !hidden.has(id)).length : 0

  function toggle(id: string, visible: boolean) {
    if (!data) return
    const nextHidden = new Set(data.hidden_sections)
    if (visible) nextHidden.delete(id)
    else nextHidden.add(id)
    restoreFocusTo.current = id
    update.mutate({ id, visible, hidden: data.sections.filter((section) => nextHidden.has(section)) })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle id={titleId}>{t('Agent sections')}</CardTitle>
        <CardDescription>
          {t('Control which sections members of this company see on each agent page in the workspace.')}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div role="status" className="text-sm text-muted-foreground empty:hidden">
          {sections.isPending ? t('Loading agent sections…') : null}
        </div>

        {sections.error ? (
          <div className="flex flex-col items-start gap-3">
            <ErrorToast
              error={sections.error}
              message={sections.error instanceof ApiError ? sections.error.message : t('Failed to load agent sections')}
            />
            <Button type="button" variant="outline" disabled={sections.isFetching} onClick={() => { void sections.refetch() }}>
              {t('Retry')}
            </Button>
          </div>
        ) : null}

        {update.error ? (
          <ErrorToast
            error={update.error}
            message={update.error instanceof ApiError ? update.error.message : t('Could not update agent sections')}
          />
        ) : null}

        {data ? (
          <>
            <ul aria-labelledby={titleId} className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {data.sections.map((id) => {
                const visible = !hidden.has(id)
                const lastVisible = visible && visibleCount === 1
                const switchId = `${switchIdPrefix}-${id}`
                return (
                  <li key={id} className="flex items-center justify-between gap-3 rounded-xl border border-border bg-card px-3.5 py-3">
                    <div className="min-w-0">
                      <Label htmlFor={switchId} className="leading-snug [overflow-wrap:anywhere]">{sectionLabel(t, id)}</Label>
                      <p className="mt-1 text-xs text-muted-foreground">{t(visible ? 'Visible' : 'Hidden')}</p>
                    </div>
                    <Switch
                      id={switchId}
                      checked={visible}
                      disabled={update.isPending || lastVisible}
                      aria-describedby={lastVisible ? hintId : undefined}
                      onCheckedChange={(next) => toggle(id, next)}
                    />
                  </li>
                )
              })}
            </ul>
            {visibleCount === 1 ? (
              <p id={hintId} className="text-sm text-muted-foreground">
                {t('At least one section must stay visible.')}
              </p>
            ) : null}
          </>
        ) : null}
      </CardContent>
    </Card>
  )
}
