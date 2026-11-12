import { zodResolver } from '@hookform/resolvers/zod'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { ApiError } from '@/lib/http'
import { fetchTenantEmail, resetTenantEmail, sendTenantEmailTest, updateTenantEmail, type TenantEmail, type TenantEmailInput } from '@/lib/tenant-email-api'
import { TenantEmailFields } from './tenant-email-fields'
import { tenantEmailDraftSchema, tenantEmailToDraft, type TenantEmailDraft } from './tenant-email-form'
import { useTranslation } from '@/hooks/use-translation'
import { useLocalizedMessage } from '@/hooks/use-localized-message'
import { message } from '@/lib/i18n'

type Props = { readonly tenantId: string; readonly saved: TenantEmail; readonly recipient: string | null }

export function TenantEmailEditor({ tenantId, saved, recipient }: Props) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const isSessionCurrent = useSessionCurrent()
  const active = useRef(false)
  const submitting = useRef(false)
  const input = useRef<TenantEmailInput | null>(null)
  const [result, setResult] = useLocalizedMessage()
  const [failure, setFailure] = useLocalizedMessage()
  const key = ['admin-tenant-email', tenantId]
  const form = useForm<TenantEmailDraft, unknown, z.output<typeof tenantEmailDraftSchema>>({ resolver: zodResolver(tenantEmailDraftSchema), defaultValues: tenantEmailToDraft(saved) })
  useEffect(() => { active.current = true; return () => { active.current = false; input.current = null } }, [])
  function begin() {
    if (submitting.current || !active.current || !isSessionCurrent()) return false
    submitting.current = true; setFailure(''); setResult('')
    return true
  }
  function finish() { submitting.current = false }
  function fail(error: Error) {
    if (active.current) setFailure(error instanceof ApiError ? error.message : message('Could not confirm the SMTP operation. Your draft is unchanged. Check the connection and try again.'))
  }
  const saving = useSessionMutation({
    mutationFn: () => {
      const payload = input.current
      input.current = null
      if (!payload) throw new Error('SMTP submission is no longer active')
      return updateTenantEmail(tenantId, payload)
    },
    retry: false, gcTime: 0,
    onMutate: () => client.cancelQueries({ queryKey: key }),
    onSuccess: (persisted) => {
      client.setQueryData(key, persisted)
      if (!active.current) return
      form.reset(tenantEmailToDraft(persisted)); setResult(message('SMTP settings saved. The password field has been cleared.'))
    },
    onError: fail, onSettled: finish,
  })
  const testing = useSessionMutation({
    mutationFn: () => sendTenantEmailTest(tenantId), retry: false, gcTime: 0,
    onSuccess: (message) => { if (active.current) setResult(message) },
    onError: fail, onSettled: finish,
  })
  const resetting = useSessionMutation({
    mutationFn: async () => {
      await resetTenantEmail(tenantId)
      return fetchTenantEmail(tenantId)
    },
    retry: false, gcTime: 0,
    onMutate: () => client.cancelQueries({ queryKey: key }),
    onSuccess: (persisted) => {
      client.setQueryData(key, persisted)
      if (!active.current) return
      form.reset(tenantEmailToDraft(persisted)); setResult(message('SMTP configuration removed. Organization email delivery is off.'))
    },
    onError: () => { if (active.current) setFailure(message('Could not confirm removal. It may already have been applied. Reload the settings before trying again.')) },
    onSettled: finish,
  })
  const pending = saving.isPending || testing.isPending || resetting.isPending
  return <form noValidate aria-label={t('Organization SMTP configuration')} aria-busy={pending} className="flex min-w-0 flex-col gap-5" onSubmit={form.handleSubmit((payload) => {
    if (!begin()) return
    input.current = payload; saving.mutate()
  })}>
    <fieldset disabled={pending} className="flex min-w-0 flex-col gap-5">
      <legend className="sr-only">{t('SMTP connection settings')}</legend>
      <TenantEmailFields form={form} hasPassword={saved.has_password} />
      <p id="smtp-test-help" className="text-sm text-muted-foreground">{t('Save changes before testing. Send test email uses only the saved SMTP configuration, even when disabled, and sends to your signed-in admin email{recipient}. Unsaved changes are not sent.', { recipient: recipient ? ` (${recipient})` : '' })}</p>
      <div className="flex flex-wrap gap-3">
        <Button type="submit" className="h-auto min-h-11 whitespace-normal">{t(saving.isPending ? 'Saving SMTP settings…' : 'Save SMTP settings')}</Button>
        <Button type="button" variant="outline" aria-describedby="smtp-test-help" className="h-auto min-h-11 whitespace-normal" disabled={!recipient} onClick={() => { if (begin()) testing.mutate() }}>{t(testing.isPending ? 'Sending test email…' : 'Send test email')}</Button>
        <Button type="button" variant="outline" className="h-auto min-h-11 whitespace-normal text-destructive hover:text-destructive" onClick={() => {
          if (submitting.current || !active.current || !isSessionCurrent()) return
          if (!window.confirm(t('Remove this organization’s SMTP configuration? Organization email delivery will stop. This removes the saved password and discards this draft.'))) return
          if (begin()) resetting.mutate()
        }}>{t(resetting.isPending ? 'Removing SMTP configuration…' : 'Remove SMTP configuration')}</Button>
      </div>
    </fieldset>
    {failure ? <ErrorToast message={failure} /> : null}
    <p role="status" className="text-sm text-muted-foreground">{pending ? t(saving.isPending ? 'Saving SMTP settings…' : testing.isPending ? 'Sending a test using the saved configuration…' : 'Removing organization SMTP…') : result}</p>
  </form>
}
