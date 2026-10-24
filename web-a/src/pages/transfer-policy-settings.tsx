import { zodResolver } from '@hookform/resolvers/zod'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useId, useRef } from 'react'
import { useForm } from 'react-hook-form'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth, useSessionCurrent } from '@/hooks/use-auth'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { ApiError } from '@/lib/http'
import { fetchTransferPolicy, updateTransferPolicy, type TransferPolicy } from '@/lib/transfer-policy-api'
import { formatMinorAmount, transferPolicyDraftSchema, transferPolicyToDraft } from './transfer-policy-form'

const policyKey = ['admin-transfer-policy'] as const

export function TransferPolicySettings() {
  const id = useId()
  const { mustChangePassword } = useAuth()
  const policy = useQuery({
    queryKey: policyKey,
    queryFn: ({ signal }) => fetchTransferPolicy(signal),
    enabled: !mustChangePassword,
    retry: false,
  })

  return (
    <Card role="region" aria-labelledby={`${id}-heading`}>
      <CardHeader>
        <h2 id={`${id}-heading`} className="font-display text-lg font-semibold">Transfer fee and instructions</h2>
        <CardDescription>
          Platform-wide policy shown to members when they create a transfer admission request.
          Payment is verified manually; these settings do not collect or confirm payment or approve an admission.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-5 [overflow-wrap:anywhere]">
        {mustChangePassword ? (
          <p className="text-sm text-muted-foreground">Change your password on <Link className="text-primary underline underline-offset-4" to="/account">Account</Link> before viewing or updating this policy.</p>
        ) : (
          <>
            {policy.isPending ? <p role="status" className="text-sm text-muted-foreground">Loading transfer policy…</p> : null}
            {policy.isError ? (
              <div className="flex flex-col items-start gap-3">
                <p role="alert" className="text-sm text-destructive">{policy.error instanceof ApiError ? policy.error.message : 'Could not load the transfer policy.'}</p>
                <Button type="button" variant="outline" disabled={policy.isFetching} onClick={() => { void policy.refetch() }}>{policy.isFetching ? 'Loading…' : 'Retry loading policy'}</Button>
              </div>
            ) : null}
            {policy.data ? <TransferPolicyForm policy={policy.data} /> : null}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function TransferPolicyForm({ policy }: { readonly policy: TransferPolicy }) {
  const id = useId()
  const queryClient = useQueryClient()
  const isSessionCurrent = useSessionCurrent()
  const active = useRef(false)
  const submitting = useRef(false)
  const form = useForm({
    resolver: zodResolver(transferPolicyDraftSchema),
    values: transferPolicyToDraft(policy),
    resetOptions: { keepDirtyValues: true },
  })
  const { errors, isDirty } = form.formState

  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])

  const saving = useSessionMutation({
    mutationFn: updateTransferPolicy,
    retry: false,
    onMutate: () => queryClient.cancelQueries({ queryKey: policyKey }),
    onSuccess: (saved) => {
      queryClient.setQueryData(policyKey, saved)
      if (!active.current) return
      form.reset(transferPolicyToDraft(saved), { keepDirtyValues: false })
      toast.success('Transfer policy saved')
    },
    onSettled: () => { submitting.current = false },
  })

  return (
    <>
      <section aria-labelledby={`${id}-saved`} className="rounded-xl border border-border bg-background p-4">
        <h3 id={`${id}-saved`} className="text-sm font-semibold">Current saved policy</h3>
        <dl className="mt-3 flex flex-col gap-3 text-sm">
          <div><dt className="text-muted-foreground">Transfer fee</dt><dd className="mt-1 font-medium tabular-nums">{policy.currency} {formatMinorAmount(policy.fee_amount_minor, policy.currency)}</dd></div>
          <div><dt className="text-muted-foreground">Payment instructions</dt><dd className="mt-1 whitespace-pre-wrap leading-relaxed">{policy.instructions}</dd></div>
        </dl>
      </section>
      <form noValidate className="flex flex-col gap-5" aria-busy={saving.isPending} onSubmit={form.handleSubmit((values) => {
        if (submitting.current || !active.current || !isSessionCurrent()) return
        submitting.current = true
        saving.mutate(values)
      })}>
        <fieldset disabled={saving.isPending} className="flex min-w-0 flex-col gap-5 disabled:opacity-60">
          <legend className="sr-only">Edit transfer policy</legend>
          <div className="grid gap-5 sm:grid-cols-2">
            <div className="flex flex-col gap-2">
              <Label htmlFor={`${id}-amount`}>Fee amount</Label>
              <Input id={`${id}-amount`} inputMode="decimal" autoComplete="off" aria-required="true" aria-invalid={Boolean(errors.amount)} aria-describedby={`${id}-amount-help ${errors.amount ? `${id}-amount-error` : ''}`} {...form.register('amount')} />
              <p id={`${id}-amount-help`} className="text-xs text-muted-foreground">USD, EUR, GBP: up to 2 decimal places. VND, JPY: whole numbers only. KWD: up to 3 decimal places. Zero is allowed.</p>
              {errors.amount ? <p id={`${id}-amount-error`} role="alert" className="text-sm text-destructive">{errors.amount.message}</p> : null}
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor={`${id}-currency`}>Currency</Label>
              <Input id={`${id}-currency`} className="uppercase" autoComplete="off" autoCapitalize="characters" spellCheck={false} aria-required="true" aria-invalid={Boolean(errors.currency)} aria-describedby={`${id}-currency-help ${errors.currency ? `${id}-currency-error` : ''}`} {...form.register('currency')} />
              <p id={`${id}-currency-help`} className="text-xs text-muted-foreground">USD, EUR, GBP, VND, JPY, or KWD. Saved in uppercase.</p>
              {errors.currency ? <p id={`${id}-currency-error`} role="alert" className="text-sm text-destructive">{errors.currency.message}</p> : null}
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor={`${id}-instructions`}>Payment instructions</Label>
            <textarea id={`${id}-instructions`} rows={6} aria-required="true" aria-invalid={Boolean(errors.instructions)} aria-describedby={`${id}-instructions-help ${errors.instructions ? `${id}-instructions-error` : ''}`} className="min-h-36 w-full min-w-0 resize-y rounded-xl border border-input bg-card px-3.5 py-2 text-base shadow-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/35 aria-invalid:border-destructive sm:text-sm" {...form.register('instructions')} />
            <p id={`${id}-instructions-help`} className="text-xs text-muted-foreground">Required. Up to 4,000 characters. Explain how members should pay and how payment will be verified manually.</p>
            {errors.instructions ? <p id={`${id}-instructions-error`} role="alert" className="text-sm text-destructive">{errors.instructions.message}</p> : null}
          </div>
          <Button type="submit" className="h-auto min-h-11 self-start whitespace-normal">{saving.isPending ? 'Saving policy…' : 'Save transfer policy'}</Button>
        </fieldset>
        {saving.isError ? <p role="alert" className="text-sm text-destructive">{saving.error instanceof ApiError ? saving.error.message : 'Could not confirm the saved policy. Your draft is kept; retry saving or reload to check the current policy.'}</p> : null}
        <p role="status" className="text-sm text-muted-foreground">{saving.isPending ? 'Saving transfer policy. Please wait.' : isDirty ? 'Unsaved changes. The current saved policy above remains in effect.' : saving.isSuccess ? 'Transfer policy saved.' : null}</p>
      </form>
    </>
  )
}
