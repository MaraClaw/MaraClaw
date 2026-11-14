import { useTranslation } from '@/hooks/use-translation'
import { ValidationMessage } from '@/components/validation-message'
import { zodResolver } from '@hookform/resolvers/zod'
import { useState } from 'react'
import { useForm, useWatch } from 'react-hook-form'

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

type BillingPeriod = 'monthly' | 'annual'

const GIB = 1024 ** 3

function annualSavingPercent(plan: OrganizationPlan): number {
  return Math.round((1 - plan.annual_price_usd / (plan.monthly_price_usd * 12)) * 100)
}

export function OrganizationAdmissionForm({ plans }: { readonly plans: readonly OrganizationPlan[] }) {
  const { t: translate, formatNumber } = useTranslation()
  const [billing, setBilling] = useState<BillingPeriod>('monthly')
  const [success, setSuccess] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { register, handleSubmit, control, formState: { errors, isSubmitting } } = useForm<OrganizationAdmission>({
    resolver: zodResolver(organizationAdmissionSchema),
    defaultValues: { organization_name: '', email: '', tier: 'pro' },
  })
  const selectedTier = useWatch({ control, name: 'tier' })
  const savingPercents = plans.map(annualSavingPercent)
  const bestSaving = Math.max(0, ...savingPercents)
  const savingLabel = savingPercents.every((percent) => percent === bestSaving)
    ? translate('Save {percent}%', { percent: formatNumber(bestSaving) })
    : translate('Save up to {percent}%', { percent: formatNumber(bestSaving) })
  const billingOptions = [
    { period: 'monthly', label: translate("Pay monthly") },
    { period: 'annual', label: translate("Pay annually") },
  ] as const

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
      <fieldset className="flex justify-center">
        <legend className="sr-only">{translate("Billing period")}</legend>
        <span className="inline-flex gap-1 rounded-full border border-border bg-card p-1 shadow-card">
          {billingOptions.map(({ period, label }) => (
            <label key={period} className="relative flex min-h-11 cursor-pointer items-center gap-2 rounded-full px-5 text-[17px] font-medium text-muted-foreground transition-[background-color,color] duration-150 hover:text-foreground has-checked:bg-primary has-checked:text-primary-foreground has-focus-visible:ring-2 has-focus-visible:ring-ring has-focus-visible:ring-offset-2 has-focus-visible:ring-offset-background sm:text-base">
              <input type="radio" name="billing-period" value={period} checked={billing === period} onChange={() => setBilling(period)} className="sr-only" />
              {label}
              {period === 'annual' && bestSaving > 0 && (
                <span className="rounded-full bg-accent px-2 py-0.5 text-sm font-semibold text-accent-foreground">{savingLabel}</span>
              )}
            </label>
          ))}
        </span>
      </fieldset>

      <fieldset disabled={isSubmitting || success} className="space-y-4">
        <legend className="mb-4 text-[17px] font-medium sm:text-base">{translate("Choose your proposed plan")}</legend>
        <div className="grid gap-4 lg:grid-cols-3">
          {plans.map((plan) => {
            const annualSaving = plan.monthly_price_usd * 12 - plan.annual_price_usd
            return (
              <label key={plan.id} className="relative flex cursor-pointer flex-col rounded-2xl border border-border bg-card p-6 shadow-card transition-colors has-checked:bg-accent/50 has-focus-visible:ring-2 has-focus-visible:ring-ring has-disabled:cursor-default">
                <span className="flex items-center justify-between gap-4">
                  <span className="font-display text-2xl font-semibold">{plan.name}</span>
                  <input {...register('tier')} type="radio" value={plan.id} aria-label={plan.name} className="size-5 accent-primary" />
                </span>
                <span className="mt-5 flex items-baseline gap-2">
                  <span className="font-display text-4xl font-semibold tracking-tight tabular-nums">
                    ${formatNumber(billing === 'annual' ? plan.annual_price_usd : plan.monthly_price_usd)}
                  </span>
                  <span className="text-muted-foreground">{billing === 'annual' ? translate("USD / year") : translate("USD / month")}</span>
                </span>
                <span className="mt-1 text-muted-foreground">
                  {billing === 'monthly'
                    ? translate("Billed monthly")
                    : annualSaving > 0
                      ? translate('Billed annually, save ${amount} USD', { amount: formatNumber(annualSaving) })
                      : translate("Billed annually")}
                </span>
                <span className="mt-5 space-y-2 text-[17px] leading-relaxed sm:text-base">
                  <span className="block">{translate('{count} human accounts included', { count: formatNumber(plan.included_accounts) })}</span>
                  <span className="block text-muted-foreground">${formatNumber(plan.additional_account_monthly_usd)} {translate("USD / additional account / month")}</span>
                  <span className="block">{translate('{count} agents included', { count: formatNumber(plan.included_agents) })}</span>
                  <span className="block text-muted-foreground">${formatNumber(plan.additional_agent_monthly_usd)} {translate("USD / additional agent / month")}</span>
                </span>
              </label>
            )
          })}
        </div>
      </fieldset>

      <div className="mx-auto max-w-3xl">
        <table className="w-full text-left text-[17px] leading-relaxed sm:text-base">
          <caption className="mb-3 text-left font-medium">{translate("Resources by plan")}</caption>
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th scope="col" className="py-2 pr-3 font-medium">{translate("Plan")}</th>
              <th scope="col" className="px-3 py-2 font-medium">{translate("vCPU per agent")}</th>
              <th scope="col" className="px-3 py-2 font-medium">{translate("Memory per agent")}</th>
              <th scope="col" className="py-2 pl-3 font-medium">{translate("Managed S3 storage")}</th>
            </tr>
          </thead>
          <tbody>
            {plans.map((plan) => (
              <tr key={plan.id} className={`border-b border-border last:border-b-0 ${plan.id === selectedTier ? 'bg-accent/50' : ''}`}>
                <th scope="row" className="py-3 pr-3 font-semibold">{plan.name}</th>
                <td className="px-3 py-3 tabular-nums">{translate('{count} vCPU', { count: formatNumber(plan.guest_millicpus / 1000) })}</td>
                <td className="px-3 py-3 tabular-nums">{translate('{size} GiB', { size: formatNumber(plan.guest_memory_bytes / GIB) })}</td>
                <td className="py-3 pl-3 tabular-nums">
                  <span className="block">{translate('{size} GiB total', { size: formatNumber(plan.storage_limit_bytes * plan.included_agents / GIB) })}</span>
                  <span className="block text-muted-foreground">{translate('{size} GiB per agent', { size: formatNumber(plan.storage_limit_bytes / GIB) })}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p id="pricing-models" className="mx-auto max-w-3xl text-center text-[17px] font-medium leading-relaxed sm:text-base">
        {translate("Bring your own AI: connect your existing Grok, ChatGPT or Claude subscription, or bring your own API key (BYOK) for Claude, GPT, Gemini, DeepSeek and more. AI model usage isn't included in plan prices; your provider bills you directly.")}</p>

      <p id="pricing-terms" className="mx-auto max-w-3xl text-center text-[17px] leading-relaxed text-muted-foreground sm:text-base">
        {translate("Plans cover your whole company, including your organization admin. Your organization admin can add accounts and agents any time in the admin console, billed monthly by bank transfer. Prices are proposals confirmed when your request is approved.")}</p>

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
