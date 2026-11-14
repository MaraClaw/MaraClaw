import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useId, useMemo } from 'react'
import { Controller, useForm } from 'react-hook-form'

import { capacityTierLabels } from '@/components/capacity/capacity-labels'
import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordField } from '@/components/ui/password-field'
import { Select } from '@/components/ui/select'
import { organizationPlansOptions } from '@/hooks/organization-plan-options'
import { useSessionCurrent } from '@/hooks/use-auth'
import { createCompany } from '@/lib/companies-api'
import { createCompanySchema, type CreateCompanyValues } from '@/lib/company-form-schema'
import { OrganizationPlanIdValues } from '@/lib/engine-contracts.generated'
import { ApiError, formatApiDetail } from '@/lib/http'
import { useTranslation } from '@/hooks/use-translation'
import { useLocalizedMessage } from '@/hooks/use-localized-message'
import { message } from '@/lib/i18n'
import { DEFAULT_PHONE_COUNTRY, phoneCountryOptions, toE164 } from '@/lib/phone'

type CreateCompanyFormProps = {
  onCreated: (name: string, adminEmail: string) => void
}

export function CreateCompanyForm({ onCreated }: CreateCompanyFormProps) {
  const { t, owned, number, locale } = useTranslation()
  const formId = useId()
  const isCurrent = useSessionCurrent()
  const [formError, setFormError] = useLocalizedMessage()
  const plans = useQuery(organizationPlansOptions())
  const countries = useMemo(() => phoneCountryOptions(locale), [locale])
  const {
    register,
    control,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting, isValid },
  } = useForm<CreateCompanyValues>({
    resolver: zodResolver(createCompanySchema),
    mode: 'onTouched',
    defaultValues: {
      name: '',
      phone_country: DEFAULT_PHONE_COUNTRY,
      phone_number: '',
      admin_email: '',
      admin_display_name: '',
      admin_password: '',
    },
  })

  async function onSubmit(values: CreateCompanyValues) {
    setFormError(null)
    const phone = toE164(values.phone_country, values.phone_number)
    if (phone === null) return
    const displayName = values.admin_display_name?.trim()
    try {
      const created = await createCompany({
        name: values.name.trim(),
        phone,
        tier: values.tier,
        admin_email: values.admin_email.trim().toLowerCase(),
        admin_password: values.admin_password,
        admin_display_name: displayName || undefined,
      })
      if (!isCurrent()) return
      reset()
      onCreated(created.company.name, created.org_admin_email)
    } catch (error) {
      if (!isCurrent()) return
      if (error instanceof ApiError) {
        if (error.status === 409) {
          setFormError(
            formatApiDetail(error.detail) ??
              message('That admin email or email domain is already registered.'),
          )
          return
        }
        if (error.status === 403) {
          setFormError(message('Only a platform admin can create a company.'))
          return
        }
        setFormError(formatApiDetail(error.detail) ?? message('Could not create company.'))
        return
      }
      setFormError(message('Could not create company. Check your connection and try again.'))
    }
  }

  const catalog = new Map((plans.data ?? []).map((plan) => [plan.id, plan]))

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('New company')}</CardTitle>
        <CardDescription>
          {t("Creates the organization and its genesis org admin. The admin email host is claimed as this company's default email domain. That admin must change the initial password on first login.")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="grid gap-4 sm:grid-cols-2" onSubmit={handleSubmit(onSubmit)} noValidate>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-name`}>{t('Company name')}</Label>
            <Input
              id={`${formId}-name`}
              autoComplete="organization"
              aria-invalid={errors.name ? true : undefined}
              {...register('name')}
            />
            {errors.name ? (
              <p className="text-xs text-destructive" role="alert">
                {owned(errors.name.message)}
              </p>
            ) : null}
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-phone`}>{t('Phone number')}</Label>
            <div className="flex gap-2">
              <Select aria-label={t('Country code')} className="w-44 shrink-0" {...register('phone_country')}>
                {countries.map(({ country, label }) => (
                  <option key={country} value={country}>
                    {label}
                  </option>
                ))}
              </Select>
              <Input
                id={`${formId}-phone`}
                type="tel"
                inputMode="tel"
                autoComplete="off"
                aria-invalid={errors.phone_number ? true : undefined}
                aria-describedby={errors.phone_number ? `${formId}-phone-error` : undefined}
                {...register('phone_number')}
              />
            </div>
            {errors.phone_number ? (
              <p id={`${formId}-phone-error`} className="text-xs text-destructive" role="alert">
                {owned(errors.phone_number.message)}
              </p>
            ) : null}
          </div>
          <fieldset
            className="min-w-0 space-y-2 sm:col-span-2"
            aria-describedby={errors.tier ? `${formId}-plan-error` : undefined}
          >
            <legend className="mb-2 text-sm font-medium">{t('Plan')}</legend>
            <Controller
              name="tier"
              control={control}
              render={({ field }) => (
                <div className="grid gap-3 sm:grid-cols-3">
                  {OrganizationPlanIdValues.map((id, index) => {
                    const plan = catalog.get(id)
                    return (
                      <label
                        key={id}
                        className="flex min-h-11 cursor-pointer items-start gap-3 rounded-xl border border-border bg-card px-3.5 py-3 text-sm has-checked:border-primary has-checked:bg-accent/50"
                      >
                        <input
                          ref={index === 0 ? field.ref : undefined}
                          type="radio"
                          name={field.name}
                          value={id}
                          checked={field.value === id}
                          onBlur={field.onBlur}
                          onChange={() => field.onChange(id)}
                          aria-describedby={plan ? `${formId}-plan-${id}-summary` : undefined}
                          className="mt-0.5 size-5 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                        />
                        <span className="min-w-0">
                          <span className="block font-medium">{capacityTierLabels[id]}</span>
                          {plan ? (
                            <span
                              id={`${formId}-plan-${id}-summary`}
                              className="mt-1 block text-xs text-muted-foreground"
                            >
                              {t('{price}/month · {accounts} accounts · {agents} agents', {
                                price: number(plan.monthly_price_usd, {
                                  style: 'currency',
                                  currency: 'USD',
                                  minimumFractionDigits: 0,
                                  maximumFractionDigits: 0,
                                }),
                                accounts: number(plan.included_accounts),
                                agents: number(plan.included_agents),
                              })}
                            </span>
                          ) : null}
                        </span>
                      </label>
                    )
                  })}
                </div>
              )}
            />
            {errors.tier ? (
              <p id={`${formId}-plan-error`} className="text-xs text-destructive" role="alert">
                {owned(errors.tier.message)}
              </p>
            ) : null}
          </fieldset>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-email`}>{t('Org admin email')}</Label>
            <Input
              id={`${formId}-email`}
              type="email"
              autoComplete="off"
              aria-invalid={errors.admin_email ? true : undefined}
              {...register('admin_email')}
            />
            {errors.admin_email ? (
              <p className="text-xs text-destructive" role="alert">
                {owned(errors.admin_email.message)}
              </p>
            ) : null}
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-display`}>{t('Admin display name (optional)')}</Label>
            <Input
              id={`${formId}-display`}
              autoComplete="off"
              {...register('admin_display_name')}
            />
            {errors.admin_display_name ? (
              <p className="text-xs text-destructive" role="alert">
                {owned(errors.admin_display_name.message)}
              </p>
            ) : null}
          </div>
          <div className="sm:col-span-2">
            <PasswordField
              id={`${formId}-password`}
              label={t('Initial admin password')}
              autoComplete="new-password"
              hideLeadingIcon
              error={errors.admin_password?.message}
              {...register('admin_password')}
            />
          </div>
          {formError ? (
            <ErrorToast message={formError} />
          ) : null}
          <div className="flex flex-wrap items-center gap-3 sm:col-span-2">
            <Button type="submit" disabled={isSubmitting || !isValid}>
              {isSubmitting ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
              {t('Create company')}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
