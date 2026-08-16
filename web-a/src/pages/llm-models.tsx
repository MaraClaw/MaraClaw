import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordField } from '@/components/ui/password-field'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/hooks/use-auth'
import { listCompanies } from '@/lib/companies-api'
import { ApiError, formatApiDetail } from '@/lib/http'
import {
  createLlmModel,
  deleteLlmModel,
  listLlmModels,
  listLlmProviders,
  setDefaultLlmModel,
  setFallbackLlmModel,
  setSecondaryLlmModel,
  testLlmModel,
  updateLlmModel,
  reasoningEffortLabel,
  reasoningEffortsFor,
  withKnownProviders,
  type LlmModel,
} from '@/lib/llm-models-api'
import { isPlatformAdminUser } from '@/lib/types/auth'
import { cn } from '@/lib/utils'

const schema = z.object({
  provider: z.string().min(1, 'Choose a provider'),
  model: z.string().trim().min(1, 'Enter a model name').max(200),
  label: z.string().trim().min(1, 'Enter a label').max(200),
  api_key: z.string().trim().min(1, 'Enter an API key').max(512),
  base_url: z.string().trim().max(500).optional(),
  temperature: z.string().optional(),
  reasoning_effort: z.enum(['none', 'low', 'medium', 'high', 'xhigh']),
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
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const formId = useId()
  const platformAdmin = isPlatformAdminUser(user)
  const [searchParams, setSearchParams] = useSearchParams()
  const companyId = searchParams.get('company') ?? ''
  const [formError, setFormError] = useState<string | null>(null)

  const tenantId = platformAdmin ? companyId : (user?.tenant_id ?? '')

  const companies = useQuery({
    queryKey: ['admin-companies'],
    queryFn: () => listCompanies(),
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
    queryFn: listLlmProviders,
  })

  const models = useQuery({
    queryKey: ['admin-llm-models', tenantId],
    queryFn: () => listLlmModels(platformAdmin ? tenantId : undefined),
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
    const currentEffort = watch('reasoning_effort')
    if (addEfforts.length > 0 && currentEffort && !addEfforts.includes(currentEffort)) {
      const next = addEfforts.includes('none') ? 'none' : addEfforts[0]
      if (next === 'none' || next === 'low' || next === 'medium' || next === 'high' || next === 'xhigh') {
        setValue('reasoning_effort', next)
      }
    }
  }, [providerMeta?.default_base_url, providerMeta?.default_model, addEfforts, setValue, watch])

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
          reasoning_effort: values.reasoning_effort || 'none',
          enabled: values.enabled,
          supports_vision: values.supports_vision,
        },
        platformAdmin ? tenantId : undefined,
      )
      reset({
        provider: values.provider,
        model: providerMeta?.default_model ?? '',
        label: defaultLabelFor(providerMeta),
        api_key: '',
        base_url: providerMeta?.default_base_url ?? '',
        temperature: '',
        reasoning_effort: values.reasoning_effort,
        enabled: true,
        supports_vision: false,
      })
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      toast.success(`Added ${created.label}`)
    } catch (error) {
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
        <h1 className="font-display text-2xl font-semibold tracking-tight">Models</h1>
        <p className="mt-2 text-muted-foreground">
          Add providers and choose the company primary, secondary, and fallback models. New agents inherit all three.
          Primary handles complex work. Secondary handles short, routine turns. Fallback is only used when the chosen
          model fails. Members cannot change these assignments.
        </p>
      </div>

      {platformAdmin ? (
        <label className="grid w-auto gap-1.5 text-sm">
          <span className="text-muted-foreground">Company</span>
          <Select
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

      <Card>
        <CardHeader>
          <CardTitle>Add model</CardTitle>
          <CardDescription>
            The API key is stored encrypted. After save, only the last four characters are shown.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4 sm:grid-cols-2" onSubmit={handleSubmit(onSubmit)} noValidate>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-provider`}>Provider</Label>
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
                  {errors.provider.message}
                </p>
              ) : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-model`}>Model ID</Label>
              <Input
                id={`${formId}-model`}
                autoComplete="off"
                placeholder={modelPlaceholder(providerMeta)}
                aria-invalid={errors.model ? true : undefined}
                {...register('model')}
              />
              {errors.model ? (
                <p className="text-xs text-destructive" role="alert">
                  {errors.model.message}
                </p>
              ) : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-label`}>Label</Label>
              <Input
                id={`${formId}-label`}
                autoComplete="off"
                placeholder={defaultLabelFor(providerMeta) || 'Display name'}
                aria-invalid={errors.label ? true : undefined}
                {...register('label')}
              />
              {errors.label ? (
                <p className="text-xs text-destructive" role="alert">
                  {errors.label.message}
                </p>
              ) : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-base`}>Base URL</Label>
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
                label="API key"
                autoComplete="off"
                hideLeadingIcon
                error={errors.api_key?.message}
                {...register('api_key')}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-temp`}>Temperature (optional)</Label>
              <Input id={`${formId}-temp`} inputMode="decimal" placeholder="0.2" {...register('temperature')} />
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-effort`}>Reasoning effort</Label>
              <Select id={`${formId}-effort`} {...register('reasoning_effort')}>
                {addEfforts.map((effort) => (
                  <option key={effort} value={effort}>
                    {reasoningEffortLabel(effort)}
                  </option>
                ))}
              </Select>
              <p className="text-xs text-muted-foreground">
                How much the model thinks before answering. None skips extra reasoning. Extra high is slower and more expensive.
              </p>
            </div>
            <div className="flex flex-col justify-end gap-2 pb-1">
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" className="size-4 rounded border-input" {...register('enabled')} />
                Enabled
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" className="size-4 rounded border-input" {...register('supports_vision')} />
                Supports vision
              </label>
            </div>
            {formError ? (
              <p className="text-sm text-destructive sm:col-span-2" role="alert">
                {formError}
              </p>
            ) : null}
            <div className="sm:col-span-2">
              <Button type="submit" disabled={isSubmitting || !tenantId}>
                {isSubmitting ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                Add model
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {models.isLoading ? <p className="text-sm text-muted-foreground">Loading models…</p> : null}
      {models.error ? (
        <p className="text-sm text-destructive">
          {models.error instanceof ApiError ? models.error.message : 'Failed to load models'}
        </p>
      ) : null}
      {!models.isLoading && tenantId && (models.data ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No models yet. Add one so agents can reply.</p>
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

function ModelCard({ item, efforts }: { item: LlmModel; efforts: string[] }) {
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
  const [pendingRemove, setPendingRemove] = useState(false)
  const [forceRemove, setForceRemove] = useState(false)
  const [removeHint, setRemoveHint] = useState<string | null>(null)

  const save = useMutation({
    mutationFn: () =>
      updateLlmModel(item.id, {
        label: label.trim(),
        model: modelName.trim(),
        base_url: baseUrl.trim() || null,
        api_key: apiKey.trim() || undefined,
        enabled,
        supports_vision: supportsVision,
        reasoning_effort: effort || 'none',
      }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      setEditing(false)
      setApiKey('')
      toast.success(`Updated ${updated.label}`)
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Could not update model')
    },
  })

  const makeDefault = useMutation({
    mutationFn: () => setDefaultLlmModel(item.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      toast.success(`${item.label} is now the primary model`)
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Could not set primary model')
    },
  })

  const makeFallback = useMutation({
    mutationFn: () => setFallbackLlmModel(item.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      toast.success(`${item.label} is now the fallback model`)
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Could not set fallback model')
    },
  })

  const makeSecondary = useMutation({
    mutationFn: () => setSecondaryLlmModel(item.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      toast.success(`${item.label} is now the secondary model`)
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Could not set secondary model')
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
        toast.success(`Connected in ${result.latency_ms} ms`)
        return
      }
      toast.error(result.error || 'The provider rejected this configuration')
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Could not test this model')
    },
  })

  const remove = useMutation({
    mutationFn: () => deleteLlmModel(item.id, forceRemove),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin-llm-models'] })
      toast.success(`Removed ${item.label}`)
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        const detail = error.detail as { message?: string; agents?: string[] } | null
        const names = Array.isArray(detail?.agents) ? detail.agents.join(', ') : ''
        setRemoveHint(names ? `${detail?.message ?? error.message}. In use by: ${names}` : error.message)
        setPendingRemove(true)
        return
      }
      toast.error(error instanceof ApiError ? error.message : 'Could not remove model')
    },
  })

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div>
          <CardTitle className="flex flex-wrap items-center gap-2">
            {item.label}
            {item.is_default ? <Badge variant="soft">Primary</Badge> : null}
            {item.is_secondary ? <Badge variant="outline">Secondary</Badge> : null}
            {item.is_fallback ? <Badge variant="outline">Fallback</Badge> : null}
            <Badge variant="outline">{reasoningEffortLabel(item.reasoning_effort || 'none')}</Badge>
            {item.enabled ? null : <Badge variant="secondary">Disabled</Badge>}
          </CardTitle>
          <CardDescription>
            {item.provider} / {item.model}
            {item.api_key_masked ? ` · key ${item.api_key_masked}` : ''}
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {editing ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor={`${formId}-label`}>Label</Label>
              <Input id={`${formId}-label`} value={label} onChange={(event) => setLabel(event.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-model`}>Model ID</Label>
              <Input
                id={`${formId}-model`}
                value={modelName}
                onChange={(event) => setModelName(event.target.value)}
              />
            </div>
            <div className="space-y-2 sm:col-span-2">
              <Label htmlFor={`${formId}-base`}>Base URL</Label>
              <Input id={`${formId}-base`} value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-effort`}>Reasoning effort</Label>
              <Select
                id={`${formId}-effort`}
                value={effort || (efforts.includes('none') ? 'none' : (efforts[0] ?? 'none'))}
                onChange={(event) => setEffort(event.target.value)}
              >
                {efforts.map((value) => (
                  <option key={value} value={value}>
                    {reasoningEffortLabel(value)}
                  </option>
                ))}
              </Select>
              <p className="text-xs text-muted-foreground">
                How much the model thinks before answering. None skips extra reasoning. Extra high is slower and more expensive.
              </p>
            </div>
            <div className="sm:col-span-2">
              <PasswordField
                id={`${formId}-key`}
                label="New API key (optional)"
                autoComplete="off"
                hideLeadingIcon
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder="Leave blank to keep the current key"
              />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 rounded border-input"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              Enabled
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 rounded border-input"
                checked={supportsVision}
                onChange={(event) => setSupportsVision(event.target.checked)}
              />
              Supports vision
            </label>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            Reasoning effort: {reasoningEffortLabel(item.reasoning_effort || 'none')}.
            {item.supports_vision ? ' Vision enabled.' : ''}
            {item.base_url ? ` Endpoint ${item.base_url}` : ' Provider default endpoint'}
          </p>
        )}

        {pendingRemove ? (
          <div className="flex flex-col gap-2">
            <p className="text-sm text-muted-foreground" role="status">
              {removeHint ?? 'Agents using this model will fall back to the company default after removal.'}
            </p>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="size-4 rounded border-input"
                checked={forceRemove}
                onChange={(event) => setForceRemove(event.target.checked)}
              />
              Remove even if agents still reference it
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
                Confirm remove
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
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            {editing ? (
              <>
                <Button type="button" size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
                  {save.isPending ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                  Save
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </>
            ) : (
              <Button type="button" variant="outline" size="sm" onClick={() => setEditing(true)}>
                Edit
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
              Test
            </Button>
            {item.enabled && !item.is_default ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={makeDefault.isPending}
                onClick={() => makeDefault.mutate()}
              >
                Set as primary
              </Button>
            ) : null}
            {item.enabled && !item.is_secondary && !item.is_default && !item.is_fallback ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={makeSecondary.isPending}
                onClick={() => makeSecondary.mutate()}
              >
                Set as secondary
              </Button>
            ) : null}
            {item.enabled && !item.is_fallback && !item.is_default && !item.is_secondary ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={makeFallback.isPending}
                onClick={() => makeFallback.mutate()}
              >
                Set as fallback
              </Button>
            ) : null}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={cn(pendingRemove && 'hidden')}
              onClick={() => setPendingRemove(true)}
            >
              Remove
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
