import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { useId, useLayoutEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { CompanyStatusIcon } from '@/components/companies/company-status-icon'
import { Badge } from '@/components/ui/badge'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { companyStatsOptions } from '@/hooks/company-options'
import { useSessionMutation as useMutation } from '@/hooks/use-session-mutation'
import {
  addEmailDomain,
  deleteEmailDomain,
  listEmailDomains,
  setDefaultEmailDomain,
  type EmailDomain,
} from '@/lib/companies-api'
import { ApiError } from '@/lib/http'
import { CompanyDeletion } from '@/pages/company-deletion'
import { useTranslation } from '@/hooks/use-translation'

export function CompanyDetailPage() {
  const { t } = useTranslation()
  const { companyId } = useParams<{ companyId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const [domain, setDomain] = useState('')
  const domainInput = useRef<HTMLInputElement>(null)
  const domainInputId = useId()

  function goBack() {
    if (location.key !== 'default') {
      navigate(-1)
      return
    }
    navigate('/companies')
  }

  const companies = useQuery(companyStatsOptions(companyId ?? ''))
  const company = companies.data
  const domains = useQuery({
    queryKey: ['email-domains', companyId],
    queryFn: ({ signal }) => listEmailDomains(companyId ?? '', signal),
    enabled: Boolean(companyId),
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['email-domains', companyId] })
  }

  const add = useMutation({
    mutationFn: () => addEmailDomain(companyId!, domain, (domains.data?.length ?? 0) === 0),
    onSuccess: () => {
      setDomain('')
      invalidate()
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : t('Could not add domain')),
  })
  const makeDefault = useMutation({
    mutationFn: (domainId: string) => setDefaultEmailDomain(companyId!, domainId),
    onSuccess: invalidate,
    onError: (error) => toast.error(error instanceof ApiError ? error.message : t('Could not update default')),
  })
  const remove = useMutation({
    mutationFn: (domainId: string) => deleteEmailDomain(companyId!, domainId),
    onSuccess: () => {
      invalidate()
      domainInput.current?.focus()
    },
    onError: (error) => toast.error(error instanceof ApiError ? error.message : t('Could not delete domain')),
  })

  if (!companyId) return null

  const companyMissing = !companies.isLoading && !companies.error && !company
  const canEditDomains = Boolean(company) && !companies.error && !domains.error

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <Button type="button" variant="outline" size="sm" className="w-fit" onClick={goBack}>
        <ArrowLeft className="size-3.5" aria-hidden />
        {t('Back')}
      </Button>

      {companies.error ? (
        <ErrorToast error={companies.error} message={companies.error instanceof ApiError ? companies.error.message : t('Failed to load company')} />
      ) : null}
      {companyMissing ? <p className="text-sm text-muted-foreground">{t('That company was not found.')}</p> : null}

      <div>
        <h1 className="flex items-center gap-2.5 font-display text-2xl font-semibold tracking-tight">
          {company ? <CompanyStatusIcon active={company.is_active} /> : null}
          {company?.name ?? t('Company')}
        </h1>
        <p className="mt-2 text-muted-foreground">
          {t('Claimed email domains route new registrations to this organization.')}
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          {company?.is_system ? <Badge variant="secondary">{t('System')}</Badge> : null}
          {company?.is_default_end_user_org ? <Badge>{t('Default for end users')}</Badge> : null}
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-4">
        <CompanyStat label={t('Total users')} value={company?.user_count} />
        <CompanyStat label={t('Active users')} value={company?.active_user_count} />
        <CompanyStat label={t('Inactive users')} value={company?.inactive_user_count} />
        <CompanyStat label={t('Agents')} value={company?.agent_count} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t('Email domains')}</CardTitle>
          <CardDescription>{t('One domain can belong to only one company. Mark one as default for invites.')}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              if (domain.trim()) add.mutate()
            }}
          >
            <div className="w-full max-w-xs space-y-2">
              <Label htmlFor={domainInputId}>{t('Domain')}</Label>
              <Input
                id={domainInputId}
                ref={domainInput}
                value={domain}
                onChange={(event) => setDomain(event.target.value)}
                placeholder="acme.com"
              />
            </div>
            <Button type="submit" disabled={!canEditDomains || add.isPending || !domain.trim()}>
              {t('Add domain')}
            </Button>
          </form>

          {domains.error ? (
            <ErrorToast error={domains.error} message={domains.error instanceof ApiError ? domains.error.message : t('Failed to load domains')} />
          ) : null}

          {domains.data?.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t('No domains yet. Add one to route new registrations to this organization.')}
            </p>
          ) : null}

          {(domains.data ?? []).map((item) => (
            <DomainRow
              key={item.id}
              item={item}
              removing={remove.isPending && remove.variables === item.id}
              onMakeDefault={() => makeDefault.mutate(item.id)}
              onRemove={() => remove.mutate(item.id)}
            />
          ))}
        </CardContent>
      </Card>
      {company && !companies.error && !company.is_system && !company.is_default_end_user_org ? (
        <CompanyDeletion key={JSON.stringify([company.id, company.name])} company={company} />
      ) : null}
    </div>
  )
}

function DomainRow({
  item,
  removing,
  onMakeDefault,
  onRemove,
}: {
  item: EmailDomain
  removing: boolean
  onMakeDefault: () => void
  onRemove: () => void
}) {
  const { t } = useTranslation()
  const [confirming, setConfirming] = useState(false)
  const confirm = useRef<HTMLParagraphElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const restore = useRef(false)
  useLayoutEffect(() => {
    if (confirming) confirm.current?.focus()
    else if (restore.current) {
      trigger.current?.focus()
      restore.current = false
    }
  }, [confirming])
  function cancel() {
    restore.current = true
    setConfirming(false)
  }
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b py-2 last:border-0">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm">{item.domain}</span>
        {item.is_default ? <Badge variant="outline">{t('Default')}</Badge> : null}
      </div>
      {confirming ? (
        <section
          aria-label={t('Remove domain confirmation')}
          className="flex flex-col gap-2"
          onKeyDown={(event) => {
            if (event.key === 'Escape' && !removing) {
              event.preventDefault()
              cancel()
            }
          }}
        >
          <p ref={confirm} tabIndex={-1} className="font-medium focus-visible:outline-2 focus-visible:outline-ring">
            {t('Remove {domain}?', { domain: item.domain })}
          </p>
          <p className="text-xs text-muted-foreground">
            {t('New registrations from this domain will stop routing to this organization.')}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" size="sm" disabled={removing} onClick={cancel}>
              {t('Cancel')}
            </Button>
            <Button type="button" variant="destructive" size="sm" disabled={removing} onClick={onRemove}>
              {t(removing ? 'Removing…' : 'Confirm remove')}
            </Button>
          </div>
        </section>
      ) : (
        <div className="flex gap-2">
          {!item.is_default ? (
            <Button variant="outline" size="sm" onClick={onMakeDefault}>
              {t('Make default')}
            </Button>
          ) : null}
          <Button ref={trigger} type="button" variant="ghost" size="sm" onClick={() => setConfirming(true)}>
            {t('Remove')}
          </Button>
        </div>
      )}
    </div>
  )
}

function CompanyStat({ label, value }: { label: string; value: number | undefined }) {
  const { number } = useTranslation()
  return (
    <div className="rounded-xl border border-border bg-card px-3.5 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 font-display text-xl font-semibold tabular-nums">
        {value == null ? '—' : number(value)}
      </p>
    </div>
  )
}
