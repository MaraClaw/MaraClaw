import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, Search } from 'lucide-react'
import { useId, useState } from 'react'
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
import { createLinkupKey, deleteLinkupKey, listLinkupKeys, type LinkupKey } from '@/lib/linkup-keys-api'
import { ApiError, formatApiDetail } from '@/lib/http'
import { cn } from '@/lib/utils'
import { SearchEngineAnalytics } from '@/pages/search-engine-analytics'

const schema = z.object({
  label: z.string().trim().min(1, 'Enter a label').max(200, 'Label is too long'),
  api_key: z.string().trim().min(1, 'Enter a Linkup API key').max(512, 'Key is too long'),
})

type FormValues = z.infer<typeof schema>

function formatWhen(value: string | null): string {
  if (!value) return 'Never'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function shortFingerprint(value: string): string {
  if (value.length <= 16) return value
  return `${value.slice(0, 8)}…${value.slice(-6)}`
}

function statusVariant(status: string): 'success' | 'secondary' | 'destructive' {
  if (status === 'active') return 'success'
  if (status === 'exhausted') return 'destructive'
  return 'secondary'
}

export function SearchEnginePage() {
  const queryClient = useQueryClient()
  const formId = useId()
  const [searchParams, setSearchParams] = useSearchParams()
  const [formError, setFormError] = useState<string | null>(null)
  const [pendingRemoveId, setPendingRemoveId] = useState<string | null>(null)
  const tab = searchParams.get('tab') === 'analytics' ? 'analytics' : 'keys'

  function setTab(next: 'keys' | 'analytics') {
    const copy = new URLSearchParams(searchParams)
    if (next === 'analytics') copy.set('tab', 'analytics')
    else {
      copy.delete('tab')
      copy.delete('company')
      copy.delete('range')
    }
    setSearchParams(copy, { replace: true })
  }

  const keys = useQuery({
    queryKey: ['admin-linkup-keys'],
    queryFn: listLinkupKeys,
  })

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { label: '', api_key: '' },
  })

  const remove = useMutation({
    mutationFn: deleteLinkupKey,
    onSuccess: (removed) => {
      setPendingRemoveId(null)
      void queryClient.invalidateQueries({ queryKey: ['admin-linkup-keys'] })
      toast.success(`Removed ${removed.label || 'Linkup key'}`)
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Could not remove key')
    },
  })

  async function onSubmit(values: FormValues) {
    setFormError(null)
    try {
      const created = await createLinkupKey({
        label: values.label.trim(),
        api_key: values.api_key.trim(),
      })
      reset()
      void queryClient.invalidateQueries({ queryKey: ['admin-linkup-keys'] })
      toast.success(`Added ${created.label || 'Linkup key'}`)
    } catch (error) {
      if (error instanceof ApiError) {
        if (error.status === 409) {
          setFormError(formatApiDetail(error.detail) ?? 'This Linkup API key is already stored.')
          return
        }
        if (error.status === 403) {
          setFormError('Only a platform admin can manage search engine keys.')
          return
        }
        setFormError(formatApiDetail(error.detail) ?? 'Could not add key.')
        return
      }
      setFormError('Could not add key. Check your connection and try again.')
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
      <div>
        <h1 className="font-display text-3xl font-semibold tracking-tight">Search engine</h1>
        <p className="mt-2 text-muted-foreground">
          {tab === 'analytics'
            ? 'System-wide and per-company search activity from billed Linkup calls.'
            : 'Linkup keys used by digital employees. When a key hits quota, the engine tries the next one and wraps back to the first.'}
        </p>
      </div>

      <div className="flex gap-2" role="tablist" aria-label="Search engine sections">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'keys'}
          className={cn(
            'rounded-xl px-3 py-1.5 text-sm font-medium',
            tab === 'keys' ? 'bg-muted text-foreground' : 'text-muted-foreground hover:bg-muted/60',
          )}
          onClick={() => setTab('keys')}
        >
          Keys
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'analytics'}
          className={cn(
            'rounded-xl px-3 py-1.5 text-sm font-medium',
            tab === 'analytics' ? 'bg-muted text-foreground' : 'text-muted-foreground hover:bg-muted/60',
          )}
          onClick={() => setTab('analytics')}
        >
          Analytics
        </button>
      </div>

      {tab === 'analytics' ? <SearchEngineAnalytics /> : null}

      {tab === 'keys' ? (
      <>
      <Card>
        <CardHeader>
          <CardTitle>Add Linkup key</CardTitle>
          <CardDescription>
            The secret is stored encrypted. The list shows a fingerprint only - you cannot read the
            key back after saving.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="grid gap-4 sm:grid-cols-2" onSubmit={handleSubmit(onSubmit)} noValidate>
            <div className="space-y-2">
              <Label htmlFor={`${formId}-label`}>Label</Label>
              <Input
                id={`${formId}-label`}
                autoComplete="off"
                placeholder="Production account"
                aria-invalid={errors.label ? true : undefined}
                {...register('label')}
              />
              {errors.label ? (
                <p className="text-xs text-destructive" role="alert">
                  {errors.label.message}
                </p>
              ) : null}
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
            {formError ? (
              <p className="text-sm text-destructive sm:col-span-2" role="alert">
                {formError}
              </p>
            ) : null}
            <div className="sm:col-span-2">
              <Button type="submit" disabled={isSubmitting}>
                {isSubmitting ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                Add key
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {keys.isLoading ? <p className="text-sm text-muted-foreground">Loading keys…</p> : null}
      {keys.error ? (
        <p className="text-sm text-destructive">
          {keys.error instanceof ApiError ? keys.error.message : 'Failed to load keys'}
        </p>
      ) : null}

      {!keys.isLoading && (keys.data ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No Linkup keys yet. Add one to enable search.</p>
      ) : null}

      <div className="grid gap-4">
        {(keys.data ?? []).map((key) => (
          <KeyCard
            key={key.id}
            item={key}
            lastKey={(keys.data ?? []).length === 1}
            pendingRemove={pendingRemoveId === key.id}
            removing={remove.isPending && remove.variables === key.id}
            onAskRemove={() => setPendingRemoveId(key.id)}
            onCancelRemove={() => setPendingRemoveId(null)}
            onConfirmRemove={() => remove.mutate(key.id)}
          />
        ))}
      </div>
      </>
      ) : null}
    </div>
  )
}

function KeyCard({
  item,
  lastKey,
  pendingRemove,
  removing,
  onAskRemove,
  onCancelRemove,
  onConfirmRemove,
}: {
  item: LinkupKey
  lastKey: boolean
  pendingRemove: boolean
  removing: boolean
  onAskRemove: () => void
  onCancelRemove: () => void
  onConfirmRemove: () => void
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4">
        <div>
          <CardTitle className="flex items-center gap-2">
            <Search className="size-4" aria-hidden />
            {item.label || 'Untitled key'}
          </CardTitle>
          <CardDescription title={item.fingerprint}>
            Position {item.position + 1} · {shortFingerprint(item.fingerprint)}
          </CardDescription>
        </div>
        <Badge variant={statusVariant(item.status)}>{item.status}</Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
          <span>Last used {formatWhen(item.last_used_at)}</span>
          <span>Added {formatWhen(item.created_at)}</span>
          {item.exhausted_until ? (
            <span>Exhausted until {formatWhen(item.exhausted_until)}</span>
          ) : null}
        </div>
        {pendingRemove ? (
          <div className="flex flex-col gap-2">
            <p className="text-sm text-muted-foreground" role="status">
              {lastKey
                ? 'This is the last key. Search will stop until you add another. In-flight research on this key will fail.'
                : 'Agents will fail over to the next key. In-flight research on this key will fail.'}
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" variant="destructive" size="sm" disabled={removing} onClick={onConfirmRemove}>
                {removing ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
                Confirm remove
              </Button>
              <Button type="button" variant="ghost" size="sm" disabled={removing} onClick={onCancelRemove}>
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div>
            <Button type="button" variant="ghost" size="sm" onClick={onAskRemove}>
              Remove
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
