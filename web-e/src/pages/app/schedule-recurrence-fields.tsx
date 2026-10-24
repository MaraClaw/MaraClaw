import { useState } from 'react'

import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import {
  describeScheduleRecurrence, encodeScheduleRecurrence, recognizeScheduleRecurrence, scheduleWeekdays,
  type ScheduleRecurrence,
} from '@/lib/schedule-recurrence'
import { timezoneLabel } from '@/lib/timezone-catalog'

const monthDays = Array.from({ length: 28 }, (_, index) => index + 1)

export function ScheduleRecurrenceFields({ cron, timezone, onChange }: {
  readonly cron: string
  readonly timezone: string
  readonly onChange: (cron: string) => void
}) {
  const scope = useWorkspaceSession()
  // Initialize on edit/reset only; typing a canonical custom cron must not change modes.
  const [recurrence, setRecurrence] = useState(() => recognizeScheduleRecurrence(cron))
  const [timeError, setTimeError] = useState(false)
  const time = 'time' in recurrence ? recurrence.time : '09:00'

  function change(next: ScheduleRecurrence) {
    setRecurrence(next)
    setTimeError(false)
    const expression = encodeScheduleRecurrence(next)
    if (expression !== null) onChange(expression)
  }

  function changeKind(kind: string) {
    switch (kind) {
      case 'daily': case 'weekdays': change({ kind, time }); break
      case 'weekly': change({ kind, time, weekday: 1 }); break
      case 'monthly': change({ kind, time, monthDay: 1 }); break
      case 'custom': change({ kind, cron }); break
    }
  }

  return (
    <div className="grid min-w-0 gap-3">
      <div className="grid min-w-0 gap-3 sm:grid-cols-2">
        <div className="grid min-w-0 gap-2">
          <Label htmlFor="schedule-repeat">Repeat</Label>
          <Select id="schedule-repeat" value={recurrence.kind} aria-describedby={recurrence.kind === 'custom' ? 'schedule-cron-help' : undefined}
            onChange={scope.effect((event) => changeKind(event.target.value))}>
            <option value="daily">Daily</option>
            <option value="weekdays">Weekdays (Mon-Fri)</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
            <option value="custom">Custom cron</option>
          </Select>
        </div>
        {'time' in recurrence ? <div className="grid min-w-0 gap-2">
          <Label htmlFor="schedule-time">Time</Label>
          <Input id="schedule-time" name="schedule-time" type="time" step="60" required value={recurrence.time}
            aria-invalid={timeError} aria-describedby={`schedule-recurrence-summary${timeError ? ' schedule-time-error' : ''}`}
            onInvalid={scope.effect((event) => { event.preventDefault(); setTimeError(true); event.currentTarget.focus() })}
            onChange={scope.effect((event) => change({ ...recurrence, time: event.target.value }))} />
        </div> : null}
      </div>
      {recurrence.kind === 'weekly' ? <div className="grid gap-2">
        <Label htmlFor="schedule-weekday">Day of week</Label>
        <Select id="schedule-weekday" value={recurrence.weekday}
          onChange={scope.effect((event) => change({ ...recurrence, weekday: Number(event.target.value) }))}>
          {scheduleWeekdays.map((day, index) => <option key={day} value={index}>{day}</option>)}
        </Select>
      </div> : null}
      {recurrence.kind === 'monthly' ? <div className="grid gap-2">
        <Label htmlFor="schedule-month-day">Day of month</Label>
        <Select id="schedule-month-day" value={recurrence.monthDay} aria-describedby="schedule-month-day-help"
          onChange={scope.effect((event) => change({ ...recurrence, monthDay: Number(event.target.value) }))}>
          {monthDays.map((day) => <option key={day} value={day}>{day}</option>)}
        </Select>
        <p id="schedule-month-day-help" className="text-xs text-muted-foreground">Days 1–28 are available every month. For other dates, use Custom cron.</p>
      </div> : null}
      {recurrence.kind === 'custom' ? <div className="grid gap-2">
        <Label htmlFor="schedule-cron">Cron</Label>
        <Input id="schedule-cron" required value={recurrence.cron} aria-describedby="schedule-cron-help"
          onChange={scope.effect((event) => change({ kind: 'custom', cron: event.target.value }))} />
        <p id="schedule-cron-help" className="text-xs text-muted-foreground">Your expression is kept exactly as entered. Switching to a simple repeat option replaces it. The server checks cron syntax when you save.</p>
      </div> : null}
      <p id="schedule-recurrence-summary" className="break-words text-sm text-muted-foreground">
        {encodeScheduleRecurrence(recurrence) === null ? 'Choose a time to complete this schedule.' : describeScheduleRecurrence(recurrence)} · {timezone ? timezoneLabel(timezone) : 'Choose a timezone'}
      </p>
      {timeError ? <p id="schedule-time-error" role="status" className="text-sm text-destructive">Choose a time before saving.</p> : null}
    </div>
  )
}
