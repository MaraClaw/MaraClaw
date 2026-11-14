import { useEffect, useId, useRef, useState, type ChangeEvent, type SyntheticEvent } from 'react'

import { ErrorToast } from '@/components/error-toast'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { useCapacityCancel, useCapacityEvidenceSubmit } from '@/hooks/use-capacity'
import { useTranslation } from '@/hooks/use-translation'
import type { CapacityOrder } from '@/lib/capacity-api'
import { capacityCancelErrorKey, capacityEvidenceErrorKey } from '@/lib/capacity-errors'
import { CAPACITY_EVIDENCE_ACCEPT, evidenceFileProblem } from '@/lib/capacity-forms'
import { formatFileSize } from '@/lib/capacity-format'
import { ApiError } from '@/lib/http'
import { capacityActionClass } from './capacity-labels'

export function CapacityEvidenceForm({ order }: { readonly order: CapacityOrder }) {
  const { t, owned, locale } = useTranslation()
  const id = useId()
  const input = useRef<HTMLInputElement>(null)
  const submitting = useRef(false)
  const submit = useCapacityEvidenceSubmit(order.id)
  const [file, setFile] = useState<File | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const failure = capacityEvidenceErrorKey(submit.error)

  function choose(event: ChangeEvent<HTMLInputElement>) {
    const chosen = event.currentTarget.files?.item(0) ?? null
    setFile(chosen)
    setProblem(chosen === null ? null : evidenceFileProblem(chosen))
  }

  function finish(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submitting.current) return
    const message = file === null ? 'Choose the file that shows your transfer.' : evidenceFileProblem(file)
    if (file === null || message !== null) {
      setProblem(message)
      input.current?.focus()
      return
    }
    submitting.current = true
    submit.mutate(file, { onSettled: () => { submitting.current = false } })
  }

  return <Card>
    <CardHeader>
      <h2 className="font-display text-lg font-semibold">{t('Attach your receipt')}</h2>
      <p className="text-sm leading-relaxed text-muted-foreground">{t('After you transfer, attach a screenshot, photo or PDF of the receipt and select Finish. A platform admin then checks the payment.')}</p>
    </CardHeader>
    <CardContent>
      <form noValidate className="flex flex-col gap-4" onSubmit={finish}>
        <div className="flex flex-col gap-2">
          <Label htmlFor={`${id}-file`}>{t('Payment evidence')}</Label>
          <input ref={input} id={`${id}-file`} type="file" accept={CAPACITY_EVIDENCE_ACCEPT} disabled={submit.isPending} onChange={choose}
            aria-invalid={problem !== null} aria-describedby={`${id}-hint${problem === null ? '' : ` ${id}-problem`}`}
            className="block w-full min-w-0 rounded-xl border border-input bg-card px-3 py-2.5 text-sm text-foreground shadow-sm file:me-3 file:rounded-lg file:border-0 file:bg-secondary file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-secondary-foreground focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/35 aria-invalid:border-destructive disabled:opacity-50" />
          <p id={`${id}-hint`} className="text-xs text-muted-foreground">{t('PNG, JPEG, WebP or PDF, up to 10 MiB.')}</p>
          {file !== null && problem === null ? <p className="text-sm tabular-nums">{t('Selected: {name} ({size})', { name: file.name, size: formatFileSize(file.size, locale) })}</p> : null}
          {problem !== null ? <p id={`${id}-problem`} role="alert" className="text-sm text-destructive">{owned(problem)}</p> : null}
        </div>
        {submit.isError ? <ErrorToast error={submit.error} message={failure !== null ? t(failure) : submit.error instanceof ApiError ? submit.error.message : t('Could not send the evidence. Check your connection and try again.')} /> : null}
        <p role="status" className="text-sm text-muted-foreground">{submit.isPending ? t('Sending your evidence...') : ''}</p>
        <Button type="submit" className={`self-start ${capacityActionClass}`} aria-busy={submit.isPending} disabled={submit.isPending}>{t(submit.isPending ? 'Sending...' : 'Finish')}</Button>
      </form>
    </CardContent>
  </Card>
}

export function CapacityCancelOrder({ order }: { readonly order: CapacityOrder }) {
  const { t } = useTranslation()
  const prompt = useRef<HTMLParagraphElement>(null)
  const cancel = useCapacityCancel(order.id)
  const [confirming, setConfirming] = useState(false)
  const failure = capacityCancelErrorKey(cancel.error)
  useEffect(() => { if (confirming) prompt.current?.focus() }, [confirming])

  return <div className="flex flex-col items-start gap-3">
    {confirming ? <fieldset className="flex min-w-0 flex-col items-start gap-3 rounded-xl border border-border bg-card p-4">
      <legend className="sr-only">{t('Cancel order')}</legend>
      <p ref={prompt} tabIndex={-1} className="text-sm leading-relaxed outline-none">{t('Cancel this order? Nothing has been charged. You can place a new order from Users at any time.')}</p>
      <div className="flex flex-wrap gap-3">
        <Button type="button" variant="destructive" className={capacityActionClass} disabled={cancel.isPending} aria-busy={cancel.isPending} onClick={() => cancel.mutate()}>{t(cancel.isPending ? 'Cancelling...' : 'Cancel order')}</Button>
        <Button type="button" variant="outline" className={capacityActionClass} disabled={cancel.isPending} onClick={() => setConfirming(false)}>{t('Keep order')}</Button>
      </div>
    </fieldset> : <Button type="button" variant="outline" className={capacityActionClass} onClick={() => setConfirming(true)}>{t('Cancel order')}</Button>}
    {cancel.isError ? <ErrorToast error={cancel.error} message={failure !== null ? t(failure) : cancel.error instanceof ApiError ? cancel.error.message : t('Could not cancel the order. Check your connection and try again.')} /> : null}
  </div>
}
