import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useLayoutEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth, useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { deleteCompany, getCompanyDeletionPreview, type CompanyStats } from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import { isPlatformAdminUser } from '@/lib/types/auth'
import { useTranslation } from '@/hooks/use-translation'

const buttonMotion = 'h-auto min-h-11 max-w-full whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

export function CompanyDeletion({ company }: { readonly company: CompanyStats }) {
  const { user } = useAuth()
  if (!isPlatformAdminUser(user)) return null
  return <PlatformAdminCompanyDeletion company={company} />
}

function PlatformAdminCompanyDeletion({ company }: { readonly company: CompanyStats }) {
  const { t, owned, number } = useTranslation()
  const [opened, setOpened] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const active = useRef(false)
  const submitting = useRef(false)
  const focusRequested = useRef(false)
  const previewStatus = useRef<HTMLDivElement>(null)
  const retryButton = useRef<HTMLButtonElement>(null)
  const confirmationInput = useRef<HTMLInputElement>(null)
  const inputId = useId()
  const isSessionCurrent = useSessionCurrent()
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  useLayoutEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])

  const preview = useQuery({
    queryKey: ['company-deletion-preview', company.id],
    queryFn: ({ signal }) => getCompanyDeletionPreview(company.id, signal),
    enabled: opened,
    retry: false,
    gcTime: 0,
    refetchOnWindowFocus: false,
  })
  const deletion = useSessionMutation({
    mutationFn: () => {
      if (!active.current) throw new ApiError(403, 'Platform admin access required')
      return deleteCompany(company.id)
    },
    onSuccess: () => {
      if (!active.current) return
      void queryClient.invalidateQueries({ queryKey: ['admin-companies'] })
      toast.success(t('Company deleted'))
      navigate('/companies', { replace: true })
    },
    onSettled: () => { submitting.current = false },
  })
  const previewReady = opened && preview.isSuccess && !preview.isFetching
  const canConfirm = previewReady && confirmation === company.name && !deletion.isPending

  useLayoutEffect(() => {
    if (!focusRequested.current || !opened) return
    if (previewReady) {
      confirmationInput.current?.focus()
      focusRequested.current = false
    } else if (preview.isError && !preview.isFetching) {
      retryButton.current?.focus()
      focusRequested.current = false
    } else {
      previewStatus.current?.focus()
    }
  }, [opened, previewReady, preview.isError, preview.isFetching])

  return (
    <Card className="min-w-0 [overflow-wrap:anywhere]">
      <CardHeader>
        <CardTitle>{t('Delete company')}</CardTitle>
        <CardDescription>
          {t('Permanently delete {name} and its data. This cannot be undone.', { name: company.name })}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!opened ? (
          <Button type="button" variant="outline" className={`w-fit ${buttonMotion}`} onClick={() => {
            if (!active.current || !isSessionCurrent()) return
            focusRequested.current = true
            setOpened(true)
          }}>
            {t('Preview deletion')}
          </Button>
        ) : null}
        <div ref={previewStatus} tabIndex={-1} role="status" className="text-sm text-muted-foreground">
          {opened && preview.isFetching ? t('Loading deletion preview…') : null}
        </div>
        {preview.error ? (
          <div className="flex flex-col items-start gap-3">
            <ErrorToast error={preview.error} message={preview.error instanceof ApiError ? preview.error.message : t('Could not load deletion preview')} />
            <Button ref={retryButton} type="button" variant="outline" className={buttonMotion} disabled={preview.isFetching} onClick={() => {
              if (!active.current || !isSessionCurrent()) return
              focusRequested.current = true
              void preview.refetch({ cancelRefetch: false })
            }}>
              {t('Retry preview')}
            </Button>
          </div>
        ) : null}
        {previewReady ? (
          <>
            <dl className="grid gap-3 sm:grid-cols-3">
              {[
                ['Users', preview.data.users_count],
                ['Agents', preview.data.agents_count],
                ['Affected schedules', preview.data.schedules_count],
               ].map(([label, count]) => (
                <div key={label} className="rounded-xl border border-border bg-card px-3.5 py-3">
                  <dt className="text-xs text-muted-foreground">{owned(String(label))}</dt>
                  <dd className="mt-1 font-display text-xl font-semibold tabular-nums">{typeof count === 'number' ? number(count) : count}</dd>
                </div>
              ))}
            </dl>
            <p id={`${inputId}-advisory`} className="text-sm text-muted-foreground">
              {t('These counts are advisory and may change before confirmation. Deletion independently rechecks permissions and company protection.')}
            </p>
            <div className="flex flex-col items-start gap-3">
              <Label htmlFor={inputId}>{t('Enter {name} to confirm', { name: company.name })}</Label>
              <Input ref={confirmationInput} id={inputId} name="company-name-confirmation" className="max-w-sm" autoComplete="off"
                aria-describedby={`${inputId}-advisory`} disabled={deletion.isPending}
                value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
              <Button type="button" variant="destructive" className={`${buttonMotion} dark:text-background`} disabled={!canConfirm} onClick={() => {
                if (!canConfirm || submitting.current || !active.current || !isSessionCurrent()) return
                submitting.current = true
                deletion.mutate()
              }}>
                {t(deletion.isPending ? 'Deleting company…' : 'Delete company permanently')}
              </Button>
            </div>
          </>
        ) : null}
        {deletion.error ? (
          <ErrorToast error={deletion.error} message={deletion.error instanceof ApiError ? deletion.error.message : t('Could not delete company')} />
        ) : null}
      </CardContent>
    </Card>
  )
}
