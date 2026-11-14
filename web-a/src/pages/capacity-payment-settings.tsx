import { zodResolver } from '@hookform/resolvers/zod'
import { useEffect, useId, useRef } from 'react'
import { useForm } from 'react-hook-form'
import { toast } from 'sonner'

import { capacityActionClass } from '@/components/capacity/capacity-labels'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useSessionCurrent } from '@/hooks/use-auth'
import { useCapacityPaymentSettings, useCapacitySettingsSave } from '@/hooks/use-capacity'
import { useTranslation } from '@/hooks/use-translation'
import type { CapacityPaymentSettings } from '@/lib/capacity-api'
import { capacityGuardKey } from '@/lib/capacity-errors'
import { capacityPaymentSettingsDraftSchema, type CapacityPaymentSettingsDraft } from '@/lib/capacity-forms'
import { ApiError } from '@/lib/http'

function toDraft(settings: CapacityPaymentSettings): CapacityPaymentSettingsDraft {
  return {
    bank_name: settings.bank_name ?? '', account_number: settings.account_number ?? '',
    branch_name: settings.branch_name ?? '', usd_vnd_rate: settings.usd_vnd_rate === null ? '' : String(settings.usd_vnd_rate),
  }
}

function SettingsForm({ settings }: { readonly settings: CapacityPaymentSettings }) {
  const { t, owned } = useTranslation()
  const id = useId()
  const isSessionCurrent = useSessionCurrent()
  const active = useRef(false)
  const submitting = useRef(false)
  const form = useForm({
    resolver: zodResolver(capacityPaymentSettingsDraftSchema),
    values: toDraft(settings),
    resetOptions: { keepDirtyValues: true },
  })
  const { errors, isDirty } = form.formState
  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])

  const saving = useCapacitySettingsSave()
  const refused = capacityGuardKey(saving.error)
  const fields = [
    { name: 'bank_name', label: 'Bank name', autoComplete: 'off', inputMode: undefined },
    { name: 'account_number', label: 'Account number', autoComplete: 'off', inputMode: undefined },
    { name: 'branch_name', label: 'Branch name', autoComplete: 'off', inputMode: undefined },
    { name: 'usd_vnd_rate', label: 'VND for 1 USD', autoComplete: 'off', inputMode: 'numeric' },
  ] as const

  return <form noValidate className="flex flex-col gap-5" aria-busy={saving.isPending} onSubmit={form.handleSubmit((values) => {
    if (submitting.current || !active.current || !isSessionCurrent()) return
    submitting.current = true
    saving.mutate({ ...values }, {
      onSuccess: (saved) => {
        if (!active.current) return
        form.reset(toDraft(saved), { keepDirtyValues: false })
        toast.success(t('Payment settings saved'))
      },
      onSettled: () => { submitting.current = false },
    })
  })}>
    <fieldset disabled={saving.isPending} className="flex min-w-0 flex-col gap-5 disabled:opacity-60">
      <legend className="sr-only">{t('Edit payment settings')}</legend>
      <div className="grid gap-5 sm:grid-cols-2">
        {fields.map(({ name, label, autoComplete, inputMode }) => <div key={name} className="flex flex-col gap-2">
          <Label htmlFor={`${id}-${name}`}>{t(label)}</Label>
          <Input id={`${id}-${name}`} inputMode={inputMode} autoComplete={autoComplete} aria-required="true" aria-invalid={Boolean(errors[name])}
            aria-describedby={errors[name] ? `${id}-${name}-error` : name === 'usd_vnd_rate' ? `${id}-rate-help` : undefined} {...form.register(name)} />
          {name === 'usd_vnd_rate' && !errors[name] ? <p id={`${id}-rate-help`} className="text-xs text-muted-foreground">{t('A whole number of dong for one US dollar, for example 25000.')}</p> : null}
          {errors[name] ? <p id={`${id}-${name}-error`} role="alert" className="text-sm text-destructive">{owned(errors[name].message)}</p> : null}
        </div>)}
      </div>
      <Button type="submit" className={`self-start ${capacityActionClass}`}>{t(saving.isPending ? 'Saving...' : 'Save payment settings')}</Button>
    </fieldset>
    {saving.isError ? <ErrorToast error={saving.error} message={refused !== null ? t(refused) : saving.error instanceof ApiError ? saving.error.message : t('Could not confirm the saved settings. Your changes are kept; retry saving or reload to check the current settings.')} /> : null}
    <p role="status" className="text-sm text-muted-foreground">{saving.isPending ? t('Saving payment settings. Please wait.') : isDirty ? t('Unsaved changes. The saved settings above stay in effect.') : ''}</p>
  </form>
}

export function CapacityPaymentSettingsSection() {
  const { t, number } = useTranslation()
  const id = useId()
  const settings = useCapacityPaymentSettings()
  return <Card role="region" aria-labelledby={`${id}-heading`}>
    <CardHeader>
      <h2 id={`${id}-heading`} className="font-display text-lg font-semibold">{t('Payment settings')}</h2>
      <CardDescription>{t('The bank account and exchange rate organization admins see on new orders. Orders already placed keep the details and rate they were placed with. Only the genesis platform admin can read or change these.')}</CardDescription>
    </CardHeader>
    <CardContent className="flex min-w-0 flex-col gap-5 [overflow-wrap:anywhere]">
      {settings.isPending ? <p role="status" className="text-sm text-muted-foreground">{t('Loading payment settings...')}</p> : null}
      {settings.isError ? <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-sm text-destructive">{settings.error instanceof ApiError ? settings.error.message : t('Could not load the payment settings. Check your connection and retry.')}</p>
        <Button type="button" variant="outline" className={capacityActionClass} disabled={settings.isFetching} onClick={() => { void settings.refetch() }}>{t('Retry')}</Button>
      </div> : null}
      {settings.data ? <>
        {settings.data.configured && settings.data.usd_vnd_rate !== null
          ? <p className="text-sm tabular-nums text-muted-foreground">{t('Saved rate: {rate} VND per USD.', { rate: number(settings.data.usd_vnd_rate) })}</p>
          : <p role="status" className="text-sm leading-relaxed">{t('Not set up yet. Organization admins cannot place orders, and monthly renewals are not created, until you save these details.')}</p>}
        <SettingsForm settings={settings.data} />
      </> : null}
    </CardContent>
  </Card>
}
