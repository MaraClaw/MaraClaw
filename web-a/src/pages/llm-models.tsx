import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { Badge } from '@/components/ui/badge'
import { ErrorToast } from '@/components/error-toast'
import { showErrorToast } from '@/lib/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordField } from '@/components/ui/password-field'
import { Select } from '@/components/ui/select'
import { companyOptions } from '@/hooks/company-options'
import { useAuth, useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation as useMutation } from '@/hooks/use-session-mutation'
import { useTranslation } from '@/hooks/use-translation'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  createLlmModel,
  deleteLlmModel,
  getGrokSubscriptionStatus,
  listLlmModels,
  listLlmProviders,
  REASONING_EFFORTS,
  setDefaultLlmModel,
  setFallbackLlmModel,
  startGrokSubscription,
  testLlmModel,
  updateLlmModel,
  reasoningEffortLabel,
  reasoningEffortsFor,
  withKnownProviders,
  type GrokSubscriptionStart,
  type LlmModel,
} from '@/lib/llm-models-api'
import { isPlatformAdminUser } from '@/lib/types/auth'
import { availableReasoningEffort, reasoningEffortWrite } from '@/lib/reasoning-efforts'
import { cn } from '@/lib/utils'
import { ChatGPTSubscriptionCard } from '@/pages/chatgpt-subscription-card'

const schema = z.object({
  provider: z.string().min(1, 'Choose a provider'),
  model: z.string().trim().min(1, 'Enter a model name').max(200),
  label: z.string().trim().min(1, 'Enter a label').max(200),
  api_key: z.string().trim().min(1, 'Enter an API key').max(512),
  base_url: z.string().trim().max(500).optional(),
  temperature: z.string().optional(),
  reasoning_effort: z.enum(REASONING_EFFORTS).or(z.literal('')),
  enabled: z.boolean(),
  supports_vision: z.boolean(),
})

type FormValues = z.infer<typeof schema>

function modelPlaceholder(provider: { default_model?: string | null } | undefined): string {
  return provider?.default_model || 'model-id'
}

function humanizeModelId(modelId: string): string {
  const leaf = modelId.split('/').pop() || modelId
  return leaf
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => {
      const lower = part.toLowerCase()
      if (lower === 'gpt') return 'GPT'
      if (lower === 'glm') return 'GLM'
      return part.charAt(0).toUpperCase() + part.slice(1)
    })
    .join(' ')
}

function defaultLabelFor(
  provider: { display_name?: string; default_model?: string | null } | undefined,
): string {
  if (provider?.default_model) return humanizeModelId(provider.default_model)
  return provider?.display_name ?? ''
}

function parseOptionalNumber(value: string | undefined): number | null {
  const trimmed = value?.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  return Number.isFinite(parsed) ? parsed : null
}

export function LlmModelsPage() {
  const { t, owned } = useTranslation()
  const { user } = useAuth()
  const isCurrent = useSessionCurrent()
  const queryClient = useQueryClient()
  const formId = useId()
  const platformAdmin = isPlatformAdminUser(user)
  const [searchParams, setSearchParams] = useSearchParams()
  const companyId = searchParams.get('company') ?? ''
  const [formError, setFormError] = useState<string | null>(null)

  const tenantId = platformAdmin ? companyId : (user?.tenant_id ?? '')

  const companies = useQuery({
    ...companyOptions(),
    enabled: platformAdmin,
  })

  useEffect(() => {
    if (!platformAdmin || companyId || !companies.data?.length) return
    const own = companies.data.find((company) => company.id === user?.tenant_id)
    const next = new URLSearchParams(searchParams)
    next.set('company', own?.id ?? companies.data[0].id)
    setSearchParams(next, { replace: true })
  }, [platformAdmin, companyId, companies.data, user?.tenant_id, searchParams, setSearchParams])

  const providers = useQuery({
    queryKey: ['admin-llm-providers'],
    queryFn: ({ signal }) => listLlmProviders(signal),
  })

  const models = useQuery({
    queryKey: ['admin-llm-models', tenantId],
    queryFn: ({ signal }) => listLlmModels(platformAdmin ? tenantId : undefined, signal),
    enabled: Boolean(tenantId),
  })

  const providerOptions = useMemo(() => withKnownProviders(providers.data ?? []), [providers.data])
  const defaultProvider = providerOptions[0]?.provider ?? 'grok'

  const {
    register,
    handleSubmit,
    reset,
    setValue,
    watch,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      provider: defaultProvider,
      model: '',
      label: '',
      api_key: '',
      base_url: '',
      temperature: '',
      reasoning_effort: 'none',
      enabled: true,
      supports_vision: false,
    },
  })

  const selectedProvider = watch('provider')
  const providerMeta = useMemo(
    () => providerOptions.find((item) => item.provider === selectedProvider),
    [providerOptions, selectedProvider],
  )
  const addEfforts = useMemo(
    () => reasoningEffortsFor(selectedProvider || defaultProvider, providerMeta?.reasoning_efforts),
    [selectedProvider, defaultProvider, providerMeta?.reasoning_efforts],
  )

  useEffect(() => {
    if (!providerOptions.length) return
    const current = watch('provider')
    if (!current || !providerOptions.some((item) => item.provider === current)) {
      setValue('provider', providerOptions[0].provider)
    }
  }, [providerOptions, setValue, watch])

  useEffect(() => {
    if (providerMeta?.default_base_url) {
      setValue('base_url', providerMeta.default_base_url)
    }
    if (providerMeta?.default_model) {
      setValue('model', providerMeta.default_model)
    }
    const nextLabel = defaultLabelFor(providerMeta)
    if (nextLabel) {
      setValue('label', nextLabel)
    }
    setValue('reasoning_effort', availableReasoningEffort(watch('reasoning_effort'), addEfforts) ?? '')
  }, [providerMeta, addEfforts, setValue, watch])

  async function onSubmit(values: FormValues) {
    setFormError(null)
    if (!tenantId) {
      setFormError('Select a company first.')
      return
    }
    try {
      const created = await createLlmModel(
        {
          provider: values.provider,
          model: values.model.trim(),
          label: values.label.trim(),
          api_key: values.api_key.trim(),
          base_url: values.base_url?.trim() || null,
          temperature: parseOptionalNumber(values.temperature),
          ...reasoningEffortWrite(values.reasoning_effort, addEfforts),
          enabled: values.enabled,
          supports_vision: values.supports_vision,
        },
        platformAdmin ? tenantId : undefined,
      )
      if (!isCurrent()) return
      reset({
        provider: values.provider,
        model: providerMeta?.default_model ?? '',
        label: defaultLabelFor(providerMeta),
        api_key: '',
        base_url: providerMeta?.default_base_url ?? '',
        temperature: '',
        reasoning_effort: availableReasoningEffort(values.reasoning_effort, addEfforts) ?? '',
        enabled: true,
        supports_vision: false,
      })
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      toast.success(t('Added {name}', { name: created.label }))
    } catch (error) {
      if (!isCurrent()) return
      if (error instanceof ApiError) {
        if (error.status === 403) {
          setFormError('Only an organization admin can configure LLM providers.')
          return
        }
        setFormError(formatApiDetail(error.detail) ?? 'Could not add this model.')
        return
      }
      setFormError('Could not add this model. Check your connection and try again.')
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
      <div>
        <h1 className="font-display text-2xl font-semibold tracking-tight">{t('Models')}</h1>
        <p className="mt-2 text-muted-foreground">
          {t('Add providers and choose the company primary and fallback models. New agents inherit both. The primary model handles every turn. The fallback is used only when the primary fails. Members cannot change these assignments.')}
        </p>
      </div>

      {platformAdmin ? (
        <label htmlFor={`${formId}-company`} className="grid w-auto gap-1.5 text-sm">
          <span className="text-muted-foreground">{t('Company')}</span>
          <Select
            id={`${formId}-company`}
            fit
            value={companyId}
            onChange={(event) => {
              const next = new URLSearchParams(searchParams)
              if (event.target.value) next.set('company', event.target.value)
              else next.delete('company')
              setSearchParams(next, { replace: true })
            }}
          >
            {(companies.data ?? []).map((company) => (
              <option key={company.id} value={company.id}>
                {company.name}
              </option>
            ))}
          </Select>
        </label>
      ) : null}

      <GrokSubscriptionCard tenantId={tenantId} platformAdmin={platformAdmin} />
      <ChatGPTSubscriptionCard key={tenantId} tenantId={tenantId} platformAdmin={platformAdmin} />
      <p className="text-sm text-muted-foreground">
        {t('A company can connect both subscriptions. Tokens stay on the server for each one.')}
      </p>

      <Card>
        <CardHeader>
          <CardTitle>{t('Add model')}</CardTitle>
          <CardDescription>
            {t('The API key is stored encrypted. After save, only the last four characters are shown.')}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4 sm:grid-cols-2" onSubmit={handleSubmit(onSubmit)} noValidate>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-provider`}>{t('Provider')}</Label>
              <Select
                id={`${formId}-provider`}
                aria-invalid={errors.provider ? true : undefined}
                {...register('provider')}
              >
                {providerOptions.map((provider) => (
                  <option key={provider.provider} value={provider.provider}>
                    {provider.display_name}
                  </option>
                ))}
              </Select>
              {errors.provider ? (
                <p className="text-xs text-destructive" role="alert">
                  {owned(errors.provider.message)}
                </p>
              ) : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-model`}>{t('Model ID')}</Label>
              <Input
                id={`${formId}-model`}
                autoComplete="off"
                placeholder={modelPlaceholder(providerMeta)}
                aria-invalid={errors.model ? true : undefined}
                {...register('model')}
              />
              {errors.model ? (
                <p className="text-xs text-destructive" role="alert">
                  {owned(errors.model.message)}
                </p>
              ) : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-label`}>{t('Label')}</Label>
              <Input
                id={`${formId}-label`}
                autoComplete="off"
                placeholder={defaultLabelFor(providerMeta) || t('Display name')}
                aria-invalid={errors.label ? true : undefined}
                {...register('label')}
              />
              {errors.label ? (
                <p className="text-xs text-destructive" role="alert">
                  {owned(errors.label.message)}
                </p>
              ) : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-base`}>{t('Base URL')}</Label>
              <Input
                id={`${formId}-base`}
                autoComplete="off"
                placeholder={providerMeta?.default_base_url ?? 'https://…'}
                {...register('base_url')}
              />
            </div>
            <div className="sm:col-span-2">
              <PasswordField
                id={`${formId}-key`}
                label={t('API key')}
                autoComplete="off"
                hideLeadingIcon
                error={owned(errors.api_key?.message)}
                {...register('api_key')}
              />
            </div>
            {addEfforts.length > 0 ? <div className="space-y-2">
              <Label htmlFor={`${formId}-effort`}>{t('Reasoning effort')}</Label>
              <Select id={`${formId}-effort`} {...register('reasoning_effort')}>
                {addEfforts.map((effort) => (
                  <option key={effort} value={effort}>
                    {owned(reasoningEffortLabel(effort))}
                  </option>
                ))}
              </Select>
              <p className="text-xs text-muted-foreground">
                {t('How much the model thinks before answering. None skips extra reasoning. Extra High is slower and more expensive.')}
              </p>
            </div> : null}
            <div className="flex flex-col justify-end gap-2 pb-1">
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" className="size-4 rounded border-input" {...register('enabled')} />
                {t('Enabled')}
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" className="size-4 rounded border-input" {...register('supports_vision')} />
                {t('Supports vision')}
              </label>
            </div>
            <details className="min-w-0 border-t border-border pt-1 sm:col-span-2">
              <summary className="min-h-11 cursor-pointer content-center rounded-md py-2 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                {t('Advanced settings')}
              </summary>
              <div className="grid gap-4 pt-2 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor={`${formId}-temp`}>{t('Temperature (optional)')}</Label>
                  <Input
                    id={`${formId}-temp`}
                    inputMode="decimal"
                    placeholder={t('Provider default')}
                    aria-describedby={`${formId}-temp-help`}
                    {...register('temperature')}
                  />
                  <p id={`${formId}-temp-help`} className="text-xs text-muted-foreground">
                    {t('Leave blank to use the provider default.')}
                  </p>
                </div>
              </div>
            </details>
            {formError ? (
              <ErrorToast message={owned(formError)} />
            ) : null}
            <div className="sm:col-span-2">
              <Button type="submit" disabled={isSubmitting || !tenantId}>
                {isSubmitting ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                {t('Add model')}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {companies.isError ? <ErrorToast error={companies.error} message={t('Could not load companies for models.')} /> : null}
      {providers.isError ? <ErrorToast error={providers.error} message={t('Could not load model providers.')} /> : null}
      {models.isLoading ? <p className="text-sm text-muted-foreground">{t('Loading models…')}</p> : null}
      {models.error ? (
        <ErrorToast error={models.error} message={models.error instanceof ApiError ? models.error.message : t('Failed to load models')} />
      ) : null}
      {!models.isLoading && tenantId && (models.data ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">{t('No models yet. Add one so agents can reply.')}</p>
      ) : null}

      <div className="grid gap-4">
        {(models.data ?? []).map((item) => (
          <ModelCard
            key={item.id}
            item={item}
            efforts={reasoningEffortsFor(
              item.provider,
              providerOptions.find((provider) => provider.provider === item.provider)?.reasoning_efforts,
            )}
          />
        ))}
      </div>
    </div>
  )
}

function GrokSubscriptionCard({
  tenantId,
  platformAdmin,
}: {
  tenantId: string
  platformAdmin: boolean
}) {
  const { t, owned } = useTranslation()
  const queryClient = useQueryClient()
  const isCurrent = useSessionCurrent()
  const [session, setSession] = useState<GrokSubscriptionStart | null>(null)
  const [startError, setStartError] = useState<string | null>(null)

  const start = useMutation({
    mutationFn: () => startGrokSubscription(platformAdmin ? tenantId : undefined),
    onSuccess: (started) => {
      setSession(started)
      setStartError(null)
    },
    onError: (error) => {
      if (error instanceof ApiError) {
        if (error.status === 403) {
          setStartError('Only an organization admin can connect a Grok subscription.')
          return
        }
        setStartError(formatApiDetail(error.detail) ?? 'Could not start Grok sign-in.')
        return
      }
      setStartError('Could not start Grok sign-in. Check your connection and try again.')
    },
  })

  const status = useQuery({
    queryKey: ['admin-grok-subscription', session?.session_id],
    queryFn: ({ signal }) => getGrokSubscriptionStatus(session?.session_id ?? '', signal),
    enabled: Boolean(session?.session_id),
    refetchInterval: (query) => {
      const current = query.state.data?.status
      if (!current || current === 'pending') {
        return Math.max((session?.interval ?? 5) * 1000, 2000)
      }
      return false
    },
  })

  useEffect(() => {
    if (!isCurrent() || status.data?.status !== 'authorized') return
    void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
    toast.success(t('Grok subscription connected. It is in the company pool.'))
    setSession(null)
  }, [status.data?.status, queryClient, isCurrent, t])

  const pending = session && (status.data?.status ?? 'pending') === 'pending'
  const failed = status.data && status.data.status !== 'pending' && status.data.status !== 'authorized'

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('Connect Grok subscription')}</CardTitle>
        <CardDescription>
          {t('Sign in with SuperGrok or X Premium. The verification code is shown here; access tokens stay on the server and are stored encrypted for this company.')}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {pending && session ? (
          <div className="grid gap-2 text-sm">
            <p>
              {t('Open')}{' '}
              <a
                className="font-medium text-primary underline underline-offset-4"
                href={session.verification_url}
                target="_blank"
                rel="noreferrer"
              >
                {session.verification_url}
              </a>
            </p>
            <p>
              {t('Enter code')}{' '}
              <span className="font-mono text-base tracking-wide">{session.user_code}</span>
            </p>
            <p className="text-muted-foreground">{t('Waiting for sign-in…')}</p>
          </div>
        ) : null}
        {failed ? (
          <p className="text-sm text-muted-foreground" role="status">
            {status.data?.status === 'expired'
              ? t('This Grok sign-in has expired. Start again to get a new sign-in code.')
              : status.data?.status === 'denied'
                ? t('Grok sign-in was denied. Start again when you are ready to try again.')
                : t('Grok sign-in could not be completed. Start again to try again.')}
          </p>
        ) : null}
        {startError ? (
          <ErrorToast error={start.error} message={owned(startError)} />
        ) : null}
        {status.isError ? <ErrorToast error={status.error} message={t('Could not check Grok sign-in. Try again.')} /> : null}
        <div>
          <Button
            type="button"
            disabled={start.isPending || !tenantId || Boolean(pending)}
            onClick={() => start.mutate()}
          >
            {start.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
            {pending ? t('Waiting for Grok') : t('Connect Grok subscription')}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function ModelCard({ item, efforts }: { item: LlmModel; efforts: readonly string[] }) {
  const { t, owned, number } = useTranslation()
  const queryClient = useQueryClient()
  const formId = useId()
  const [editing, setEditing] = useState(false)
  const [label, setLabel] = useState(item.label)
  const [modelName, setModelName] = useState(item.model)
  const [baseUrl, setBaseUrl] = useState(item.base_url ?? '')
  const [apiKey, setApiKey] = useState('')
  const [enabled, setEnabled] = useState(item.enabled)
  const [supportsVision, setSupportsVision] = useState(item.supports_vision)
  const [effort, setEffort] = useState(item.reasoning_effort || 'none')
  useEffect(() => {
    setEffort((current) => availableReasoningEffort(current, efforts) ?? '')
  }, [efforts])
  const [pendingRemove, setPendingRemove] = useState(false)
  const [forceRemove, setForceRemove] = useState(false)
  const [removeHint, setRemoveHint] = useState<string | null>(null)
  const subscription =
    item.auth_kind === 'chatgpt_subscription' || item.auth_kind === 'grok_subscription'

  const save = useMutation({
    mutationFn: () =>
      updateLlmModel(item.id, {
        label: label.trim(),
        model: modelName.trim(),
        ...(subscription
          ? {}
          : {
              base_url: baseUrl.trim() || null,
              api_key: apiKey.trim() || undefined,
            }),
        enabled,
        supports_vision: supportsVision,
        ...reasoningEffortWrite(effort, efforts),
      }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      setEditing(false)
      setApiKey('')
      toast.success(t('Updated {name}', { name: updated.label }))
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : t('Could not update model'))
    },
  })

  const makeDefault = useMutation({
    mutationFn: () => setDefaultLlmModel(item.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      toast.success(t('{name} is now the primary model', { name: item.label }))
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : t('Could not set primary model'))
    },
  })

  const makeFallback = useMutation({
    mutationFn: () => setFallbackLlmModel(item.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      toast.success(t('{name} is now the fallback model', { name: item.label }))
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : t('Could not set fallback model'))
    },
  })

  const probe = useMutation({
    mutationFn: () =>
      testLlmModel({
        provider: item.provider,
        model: item.model,
        api_key: apiKey.trim() || undefined,
        base_url: baseUrl.trim() || item.base_url,
        model_id: item.id,
      }),
    onSuccess: (result) => {
      if (result.success) {
        toast.success(t('Connected in {milliseconds} ms', { milliseconds: number(result.latency_ms) }))
        return
      }
      showErrorToast(result.error || t('The provider rejected this configuration'))
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : t('Could not test this model'))
    },
  })

  const remove = useMutation({
    mutationFn: () => deleteLlmModel(item.id, forceRemove),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      toast.success(t('Removed {name}', { name: item.label }))
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        const parsed = z.object({ message: z.string().optional(), agents: z.array(z.string()).optional() }).safeParse(error.detail)
        const names = parsed.success ? parsed.data.agents?.join(', ') : undefined
        setRemoveHint(names && parsed.success ? `${parsed.data.message ?? error.message}. ${t('In use by: {names}', { names })}` : error.message)
        setPendingRemove(true)
        return
      }
      toast.error(error instanceof ApiError ? error.message : t('Could not remove model'))
    },
  })

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div>
          <CardTitle className="flex flex-wrap items-center gap-2">
            {item.label}
            {item.is_default ? <Badge variant="soft">{t('Primary')}</Badge> : null}
            {item.is_fallback ? <Badge variant="outline">{t('Fallback')}</Badge> : null}
            {item.auth_kind === 'grok_subscription' ? <Badge variant="soft">{t('Grok subscription')}</Badge> : null}
            {item.auth_kind === 'chatgpt_subscription' ? <Badge variant="soft">{t('ChatGPT subscription')}</Badge> : null}
            <Badge variant="outline">{owned(reasoningEffortLabel(item.reasoning_effort || 'none'))}</Badge>
            {item.enabled ? null : <Badge variant="secondary">{t('Disabled')}</Badge>}
          </CardTitle>
          <CardDescription>
            {item.auth_kind === 'chatgpt_subscription'
              ? `${t('ChatGPT subscription')} · ${item.model}`
              : item.auth_kind === 'grok_subscription'
                ? `${t('Grok subscription')} · ${item.model}`
                : `${item.provider} / ${item.model}`}
            {item.auth_kind !== 'chatgpt_subscription' &&
            item.auth_kind !== 'grok_subscription' &&
            item.api_key_masked
              ? ` · ${t('key {key}', { key: item.api_key_masked })}`
              : ''}
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {editing ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor={`${formId}-label`}>{t('Label')}</Label>
              <Input id={`${formId}-label`} value={label} onChange={(event) => setLabel(event.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-model`}>{t('Model ID')}</Label>
              <Input
                id={`${formId}-model`}
                value={modelName}
                onChange={(event) => setModelName(event.target.value)}
              />
            </div>
            {subscription ? null : (
              <div className="space-y-2 sm:col-span-2">
                <Label htmlFor={`${formId}-base`}>{t('Base URL')}</Label>
                <Input id={`${formId}-base`} value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
              </div>
            )}
            {efforts.length > 0 ? <div className="space-y-2">
              <Label htmlFor={`${formId}-effort`}>{t('Reasoning effort')}</Label>
              <Select
                id={`${formId}-effort`}
                value={availableReasoningEffort(effort, efforts) ?? ''}
                onChange={(event) => setEffort(event.target.value)}
              >
                {efforts.map((value) => (
                  <option key={value} value={value}>
                    {owned(reasoningEffortLabel(value))}
                  </option>
                ))}
              </Select>
              <p className="text-xs text-muted-foreground">
                {t('How much the model thinks before answering. None skips extra reasoning. Extra High is slower and more expensive.')}
              </p>
            </div> : null}
            {subscription ? null : (
              <div className="sm:col-span-2">
                <PasswordField
                  id={`${formId}-key`}
                  label={t('New API key (optional)')}
                  autoComplete="off"
                  hideLeadingIcon
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder={t('Leave blank to keep the current key')}
                />
              </div>
            )}
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 rounded border-input"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              {t('Enabled')}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 rounded border-input"
                checked={supportsVision}
                onChange={(event) => setSupportsVision(event.target.checked)}
              />
              {t('Supports vision')}
            </label>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            {t('Reasoning effort: {effort}.', { effort: owned(reasoningEffortLabel(item.reasoning_effort || 'none')) })}
            {item.supports_vision ? ` ${t('Vision enabled.')}` : ''}
            {subscription
              ? ''
              : item.base_url
                ? ` ${t('Endpoint {url}', { url: item.base_url })}`
                : ` ${t('Provider default endpoint')}`}
          </p>
        )}

        {pendingRemove ? (
          <div className="flex flex-col gap-2">
            <p className="text-sm text-muted-foreground" role="status">
              {removeHint ?? t('Agents using this model will fall back to the company default after removal.')}
            </p>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 rounded border-input"
                checked={forceRemove}
                onChange={(event) => setForceRemove(event.target.checked)}
              />
              {t('Remove even if agents still reference it')}
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="destructive"
                size="sm"
                disabled={remove.isPending}
                onClick={() => remove.mutate()}
              >
                {remove.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                {t('Confirm remove')}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={remove.isPending}
                onClick={() => {
                  setPendingRemove(false)
                  setForceRemove(false)
                  setRemoveHint(null)
                }}
              >
                {t('Cancel')}
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            {editing ? (
              <>
                <Button type="button" size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
                  {save.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                  {t('Save')}
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(false)}>
                  {t('Cancel')}
                </Button>
              </>
            ) : (
              <Button type="button" variant="outline" size="sm" onClick={() => setEditing(true)}>
                {t('Edit')}
              </Button>
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={probe.isPending}
              onClick={() => probe.mutate()}
            >
              {probe.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
              {t('Test')}
            </Button>
            {item.enabled && !item.is_default ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={makeDefault.isPending}
                onClick={() => makeDefault.mutate()}
              >
                {t('Set as primary')}
              </Button>
            ) : null}
            {item.enabled && !item.is_fallback && !item.is_default ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={makeFallback.isPending}
                onClick={() => makeFallback.mutate()}
              >
                {t('Set as fallback')}
              </Button>
            ) : null}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={cn(pendingRemove && 'hidden')}
              onClick={() => setPendingRemove(true)}
              aria-label={t('Remove {name}', { name: item.label })}
            >
              {t('Remove')}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
