import { useTranslation } from '@/hooks/use-translation'
import { useId, useState } from 'react'
import { toast } from 'sonner'

import { TimezoneSelect } from '@/components/timezone-select'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/hooks/use-auth'
import { updateCurrentUser } from '@/lib/auth-api'
import { userFacingRequestError } from '@/lib/http'
import { scheduleTimezoneError } from '@/lib/schedule-time'

export function SettingsTimezoneCard() {
  const { t: translate } = useTranslation()
  const { user, owner, isCurrent, refreshUser } = useAuth()
  const [mountedOwner] = useState(() => owner)
  const id = useId()
  const [timezone, setTimezone] = useState(() => user?.timezone ?? 'UTC')
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState<{ readonly kind: 'success' | 'error'; readonly message: string } | null>(null)

  async function save() {
    const expectedOwner = mountedOwner
    if (!isCurrent(expectedOwner) || pending) return
    const error = scheduleTimezoneError(timezone)
    if (error) { setFeedback({ kind: 'error', message: error }); return }
    setPending(true)
    setFeedback(null)
    try {
      await updateCurrentUser({ timezone }, expectedOwner)
      if (!isCurrent(expectedOwner) || !await refreshUser(expectedOwner) || !isCurrent(expectedOwner)) return
      setFeedback({ kind: 'success', message: 'Timezone updated. New schedules will use this preference.' })
    } catch (error) {
      if (!isCurrent(expectedOwner)) return
      toast.error(userFacingRequestError(error, translate("Unable to update timezone. Try again.")))
    } finally {
      if (isCurrent(expectedOwner)) setPending(false)
    }
  }

  return <Card>
    <CardHeader>
      <CardTitle>{translate("Timezone")}</CardTitle>
      <CardDescription>{translate("Set the default for new schedules. Existing schedules keep their saved timezone.")}</CardDescription>
    </CardHeader>
    <CardContent>
      <form className="space-y-4" aria-busy={pending} onSubmit={(event) => { event.preventDefault(); void save() }}>
        <div className="space-y-2">
          <Label htmlFor={`${id}-timezone`}>{translate("Timezone")}</Label>
          <TimezoneSelect id={`${id}-timezone`} value={timezone} disabled={pending}
            aria-invalid={feedback?.kind === 'error'} aria-describedby={`${id}-help${feedback ? ` ${id}-feedback` : ''}`}
            onChange={(event) => {
              if (!isCurrent(mountedOwner)) return
              setTimezone(event.target.value)
              setFeedback(null)
            }} />
          <p id={`${id}-help`} className="text-xs text-muted-foreground">{translate("Whole-hour offsets only, from GMT -12 to GMT +14. Fixed offsets do not change for daylight saving time. Saved regional timezones are preserved. Without a preference, new schedules use GMT +0 (UTC).")}</p>
        </div>
        <Button type="submit" disabled={pending}>{pending ? translate("Saving timezone...") : translate("Save timezone")}</Button>
        {feedback ? <p id={`${id}-feedback`} role="status" className={feedback.kind === 'error' ? 'text-sm text-destructive' : 'text-sm text-muted-foreground'}>{feedback.message}</p> : null}
      </form>
    </CardContent>
  </Card>
}
