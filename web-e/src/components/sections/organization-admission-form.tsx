import { useTranslation } from '@/hooks/use-translation'
import { ValidationMessage } from '@/components/validation-message'
import { zodResolver } from '@hookform/resolvers/zod'
import { useState } from 'react'
import { useForm } from 'react-hook-form'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { userFacingRequestError } from '@/lib/http'
import { authSession } from '@/lib/auth-session'
import {
  organizationAdmissionSchema,
  requestOrganizationAdmission,
} from '@/lib/organization-admissions-api'
import type { OrganizationAdmission, OrganizationPlan } from '@/lib/organization-admissions-api'

export function OrganizationAdmissionForm({ plans }: { readonly plans: readonly OrganizationPlan[] }) {
  const { t: translate, formatNumber } = useTranslation()
  const [success, setSuccess] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<OrganizationAdmission>({
    resolver: zodResolver(organizationAdmissionSchema),
    defaultValues: { organization_name: '', email: '', tier: 'pro' },
  })

  const submit = handleSubmit(async (values) => {
    const owner = authSession.capture()
    setError(null)
    try {
      await requestOrganizationAdmission(values)
      if (authSession.isCurrent(owner)) setSuccess(true)
    } catch (cause) {
      if (authSession.isCurrent(owner)) setError(userFacingRequestError(cause, translate("We could not send your request. Please try again.")))
    }
  })

  return (
    <form onSubmit={submit} noValidate aria-label={translate("Organization creation request")} className="mt-10 space-y-6">
      <fieldset disabled={isSubmitting || success} className="space-y-4">
        <legend className="mb-4 text-[17px] font-medium sm:text-base">{translate("Choose your proposed plan")}</legend>
        <div className="grid gap-4 lg:grid-cols-3">
          {plans.map((plan) => (
            <label key={plan.id} className="relative flex cursor-pointer flex-col rounded-2xl border border-border bg-card p-6 shadow-card transition-colors has-checked:bg-accent/50 has-focus-visible:ring-2 has-focus-visible:ring-ring has-disabled:cursor-default">
              <span className="flex items-center justify-between gap-4">
                <span className="font-display text-2xl font-semibold">{plan.name}</span>
                <input {...register('tier')} type="radio" value={plan.id} aria-label={plan.name} className="size-5 accent-primary" />
              </span>
              <span className="mt-5 flex items-baseline gap-2">
                <span className="font-display text-4xl font-semibold tracking-tight">${formatNumber(plan.monthly_price_usd)}</span>
                <span className="text-muted-foreground">{translate("USD / month")}</span>
              </span>
              <span className="mt-5 space-y-2 text-[17px] leading-relaxed sm:text-base">
                <span className="block">{translate('{count} human accounts included', { count: formatNumber(plan.included_accounts) })}</span>
                <span className="block text-muted-foreground">${formatNumber(plan.additional_account_monthly_usd)} {translate("USD / additional account / month")}</span>
                <span className="block">{translate('Up to {count} human accounts', { count: formatNumber(plan.max_accounts) })}</span>
                <span className="block">{translate('Up to {count} agents per account', { count: formatNumber(plan.max_agents_per_account) })}</span>
                <span className="block">{translate('Up to {count} messages per agent per day', { count: formatNumber(plan.daily_messages_per_agent) })}</span>
                <span className="block">{translate('{size} GiB managed S3 storage per agent', { size: formatNumber(plan.storage_limit_bytes / (1024 ** 3)) })}</span>
                <span className="block">{translate('{size} MiB per managed S3 file', { size: formatNumber(plan.max_file_size_bytes / (1024 ** 2)) })}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <p id="pricing-terms" className="mx-auto max-w-3xl text-center text-[17px] leading-relaxed text-muted-foreground sm:text-base">
        {translate("Human accounts include your organization admin. Each account, including additional accounts and organization admins, has its own agent allowance; agents are not pooled. Agent creation caps and daily message caps per agent are enforced for approved tiered organizations; an agent's daily count resets at midnight in your organization admin's timezone (UTC if none is set). Existing quotas remain for organizations without a tier. Human account allocations and monthly USD prices remain proposals subject to approval, not enforced quotas or paid activation. Your organization pays its own model and tool providers.")}</p>

      <p className="mx-auto max-w-3xl text-center text-[17px] leading-relaxed text-muted-foreground sm:text-base">
        {translate("Storage allowances apply to managed S3 uploads/files only. Direct guest workspace/local writes are excluded. Some upload tools have stricter limits.")}</p>

      <div className="rounded-2xl border border-border bg-card p-6 shadow-card">
        <h3 className="font-display text-xl font-semibold">{translate("Create a new organization")}</h3>
        <p className="mt-2 text-[17px] leading-relaxed text-muted-foreground sm:text-base">
          {translate("Submit your organization name, email and selected plan for review. Approval creates your organization and sends the genesis admin's initial login credentials to this email. The first login requires a password change. Payment is separate; none is collected here.")}</p>
        <fieldset disabled={isSubmitting || success} className="mt-5 flex min-w-0 flex-col gap-4 sm:flex-row sm:items-end">
          <div className="min-w-0 flex-1 space-y-2">
            <Label htmlFor="organization-name" className="text-[17px] sm:text-base">{translate("Organization name")}</Label>
            <Input {...register('organization_name')} id="organization-name" autoComplete="organization" maxLength={200} required
              aria-invalid={Boolean(errors.organization_name)} aria-describedby={errors.organization_name ? 'organization-name-error pricing-terms' : 'pricing-terms'}
              className="text-[17px] sm:text-base" />
            {errors.organization_name && <p id="organization-name-error" role="alert" className="text-[17px] text-destructive sm:text-base"><ValidationMessage message={errors.organization_name.message} /></p>}
          </div>
          <div className="min-w-0 flex-1 space-y-2">
            <Label htmlFor="organization-email" className="text-[17px] sm:text-base">{translate("Email address")}</Label>
            <Input {...register('email')} id="organization-email" type="email" autoComplete="email" maxLength={254}
              aria-invalid={Boolean(errors.email)} aria-describedby={errors.email ? 'organization-email-error pricing-terms' : 'pricing-terms'}
              className="text-[17px] sm:text-base" />
            {errors.email && <p id="organization-email-error" role="alert" className="text-[17px] text-destructive sm:text-base"><ValidationMessage message={errors.email.message} /></p>}
          </div>
          <Button type="submit" className="min-h-11 shrink-0 text-base">
            {isSubmitting ? translate("Sending request...") : translate("Create organization")}
          </Button>
        </fieldset>
        {error && <RequestErrorToast message={error} />}
        <div role="status" aria-live="polite" aria-atomic="true" className="mt-4 text-[17px] leading-relaxed sm:text-base">
          {isSubmitting && translate("Sending your organization request.")}
          {success && translate("Your request is pending genesis platform-admin review. Once approved, your organization will be created and the genesis admin's initial login credentials will be emailed to the address you submitted. The first login requires a password change. Payment is separate.")}
        </div>
      </div>
    </form>
  )
}
