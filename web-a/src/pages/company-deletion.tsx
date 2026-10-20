import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useId, useLayoutEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth, useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { deleteCompany, getCompanyDeletionPreview, type CompanyStats } from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import { isPlatformAdminUser } from '@/lib/types/auth'

const buttonMotion = 'h-auto min-h-11 max-w-full whitespace-normal motion-reduce:active:scale-100 motion-reduce:transition-none'

export function CompanyDeletion({ company }: { readonly company: CompanyStats }) {
  const { user } = useAuth()
  if (!isPlatformAdminUser(user)) return null
  return <PlatformAdminCompanyDeletion company={company} />
}

function PlatformAdminCompanyDeletion({ company }: { readonly company: CompanyStats }) {
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
      toast.success('Company deleted')
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
        <CardTitle>Delete company</CardTitle>
        <CardDescription>
          Permanently delete {company.name} and its data. This cannot be undone.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!opened ? (
          <Button type="button" variant="outline" className={`w-fit ${buttonMotion}`} onClick={() => {
            if (!active.current || !isSessionCurrent()) return
            focusRequested.current = true
            setOpened(true)
          }}>
            Preview deletion
          </Button>
        ) : null}
        <div ref={previewStatus} tabIndex={-1} role="status" className="text-sm text-muted-foreground">
          {opened && preview.isFetching ? 'Loading deletion preview…' : null}
        </div>
        {preview.error ? (
          <div className="flex flex-col items-start gap-3">
            <p role="alert" className="text-sm text-destructive">
              Preview failed: {preview.error instanceof ApiError ? preview.error.message : 'Could not load deletion preview'}
            </p>
            <Button ref={retryButton} type="button" variant="outline" className={buttonMotion} disabled={preview.isFetching} onClick={() => {
              if (!active.current || !isSessionCurrent()) return
              focusRequested.current = true
              void preview.refetch({ cancelRefetch: false })
            }}>
              Retry preview
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
                  <dt className="text-xs text-muted-foreground">{label}</dt>
                  <dd className="mt-1 font-display text-xl font-semibold tabular-nums">{count.toLocaleString()}</dd>
                </div>
              ))}
            </dl>
            <p id={`${inputId}-advisory`} className="text-sm text-muted-foreground">
              These counts are advisory and may change before confirmation. Deletion independently rechecks permissions and company protection.
            </p>
            <div className="flex flex-col items-start gap-3">
              <Label htmlFor={inputId}>Enter {company.name} to confirm</Label>
              <Input ref={confirmationInput} id={inputId} name="company-name-confirmation" className="max-w-sm" autoComplete="off"
                aria-describedby={`${inputId}-advisory`} disabled={deletion.isPending}
                value={confirmation} onChange={(event) => setConfirmation(event.target.value)} />
              <Button type="button" variant="destructive" className={`${buttonMotion} dark:text-background`} disabled={!canConfirm} onClick={() => {
                if (!canConfirm || submitting.current || !active.current || !isSessionCurrent()) return
                submitting.current = true
                deletion.mutate()
              }}>
                {deletion.isPending ? 'Deleting company…' : 'Delete company permanently'}
              </Button>
            </div>
          </>
        ) : null}
        {deletion.error ? (
          <p role="alert" className="text-sm text-destructive">
            Deletion failed: {deletion.error instanceof ApiError ? deletion.error.message : 'Could not delete company'}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}
