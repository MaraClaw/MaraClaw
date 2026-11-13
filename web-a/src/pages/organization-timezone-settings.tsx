import { useId, useState } from 'react'

import { ErrorToast } from '@/components/error-toast'
import { TimezoneSelect } from '@/components/timezone-select'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { useTranslation } from '@/hooks/use-translation'
import { updateMyTimezone } from '@/lib/auth-api'
import { canManageOrganizationTimezone, organizationTimezoneError } from '@/lib/organization-timezone'
import { timezoneLabel } from '@/lib/timezone-catalog'

export function OrganizationTimezoneSettings() {
  const { t } = useTranslation()
  const { user, mustChangePassword } = useAuth()
  const title = useId()
  if (!canManageOrganizationTimezone(user)) return null
  return <Card role="region" aria-labelledby={title}>
    <CardHeader>
      <h2 id={title} className="text-lg font-semibold leading-none">{t('Organization timezone')}</h2>
      <CardDescription>{t('Daily message limits for every agent in your organization reset at midnight in this timezone. Changing it moves the reset time immediately. Only the organization’s genesis administrator can change it.')}</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-5 [overflow-wrap:anywhere]">
      {mustChangePassword
        ? <p className="text-sm text-muted-foreground"><a href="#change-password" className="text-primary underline underline-offset-4">{t('Change your password')}</a> {t('before changing the organization timezone.')}</p>
        : <OrganizationTimezoneForm />}
    </CardContent>
  </Card>
}

function OrganizationTimezoneForm() {
  const { t, owned } = useTranslation()
  const { user, refreshUser } = useAuth()
  const id = useId()
  const stored = user?.timezone || null
  const current = stored ?? 'UTC'
  const [draft, setDraft] = useState<string | null>(null)
  const selected = draft ?? current
  const save = useSessionMutation({
    mutationFn: (timezone: string) => updateMyTimezone(timezone),
    retry: false,
    gcTime: 0,
    scope: { id: 'organization-timezone' },
    onSuccess: async () => {
      await refreshUser()
      setDraft(null)
    },
  })
  const changed = selected !== current
  const failure = save.isError ? owned(organizationTimezoneError(save.error)) : ''

  return <form noValidate aria-busy={save.isPending} className="flex min-w-0 flex-col gap-5" onSubmit={(event) => {
    event.preventDefault()
    if (changed && !save.isPending) save.mutate(selected)
  }}>
    <div className="flex min-w-0 flex-col gap-1">
      <p className="text-sm font-medium">{t('Current timezone: {timezone}', { timezone: timezoneLabel(current) })}</p>
      {stored === null ? <p className="text-sm text-muted-foreground">{t('No timezone is set, so UTC is used.')}</p> : null}
    </div>
    <div className="flex min-w-0 flex-col gap-2">
      <Label htmlFor={`${id}-timezone`}>{t('Timezone')}</Label>
      <TimezoneSelect
        id={`${id}-timezone`} value={selected} disabled={save.isPending}
        aria-invalid={save.isError} aria-describedby={failure ? `${id}-error` : undefined}
        onChange={(event) => { setDraft(event.target.value); save.reset() }} />
      <p id={`${id}-error`} role="alert" className="text-sm text-destructive empty:-mt-2">{failure}</p>
    </div>
    {save.isError ? <ErrorToast error={save.error} message={failure} /> : null}
    <div className="flex flex-wrap items-center gap-3">
      <Button type="submit" className="h-auto min-h-11 whitespace-normal" disabled={!changed || save.isPending}>
        {save.isPending ? t('Saving timezone…') : t('Save timezone')}
      </Button>
      <p role="status" className="min-w-0 text-sm text-muted-foreground">
        {save.isPending ? t('Saving timezone…') : save.isSuccess ? t('Timezone saved. Daily message limits now reset at midnight in {timezone}.', { timezone: timezoneLabel(save.variables ?? current) }) : ''}
      </p>
    </div>
  </form>
}
