import { useEffect, useEffectEvent, useState } from 'react'

import { capacityActionClass } from '@/components/capacity/capacity-labels'
import { Button } from '@/components/ui/button'
import { useTranslation } from '@/hooks/use-translation'
import { ApiError, BinaryLimitError } from '@/lib/http'

type Evidence = { readonly filename: string; readonly content_type: string }
type EvidenceLoader = (signal: AbortSignal) => Promise<Uint8Array<ArrayBuffer>>
type Preview =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly url: string }
  | { readonly status: 'error'; readonly error: unknown }

const imageTypes: readonly string[] = ['image/png', 'image/jpeg', 'image/webp']

function EvidenceViewer({ load, evidence, onRetry }: {
  readonly load: EvidenceLoader; readonly evidence: Evidence; readonly onRetry: () => void
}) {
  const { t } = useTranslation()
  const [preview, setPreview] = useState<Preview>({ status: 'loading' })
  const isImage = imageTypes.includes(evidence.content_type)
  const loadBytes = useEffectEvent(load)

  useEffect(() => {
    const controller = new AbortController()
    let objectUrl: string | null = null
    loadBytes(controller.signal).then((bytes) => {
      if (controller.signal.aborted) return
      objectUrl = URL.createObjectURL(new Blob([bytes], { type: evidence.content_type }))
      setPreview({ status: 'ready', url: objectUrl })
    }, (error: unknown) => {
      if (!controller.signal.aborted) setPreview({ status: 'error', error })
    })
    return () => {
      controller.abort()
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl)
    }
  }, [evidence.content_type])

  return <div className="flex flex-col items-start gap-3">
    {preview.status === 'loading' ? <p role="status" className="text-sm text-muted-foreground">{t('Loading payment evidence...')}</p> : null}
    {preview.status === 'error' ? <>
      <p role="alert" className="text-sm text-destructive">{preview.error instanceof BinaryLimitError
        ? t('This file is too large to preview.')
        : preview.error instanceof ApiError && preview.error.status === 404
          ? t('The payment evidence is no longer available.')
          : t('Could not load the payment evidence. Check your connection and retry.')}</p>
      <Button type="button" variant="outline" className={capacityActionClass} onClick={onRetry}>{t('Retry')}</Button>
    </> : null}
    {preview.status === 'ready' && isImage ? <img src={preview.url} alt={t('Payment evidence: {name}', { name: evidence.filename })} className="max-h-[70vh] w-auto max-w-full rounded-xl border border-border bg-card object-contain" /> : null}
    {preview.status === 'ready' && !isImage ? <div className="flex flex-wrap gap-3">
      <Button asChild variant="outline" className={capacityActionClass}>
        <a href={preview.url} target="_blank" rel="noopener noreferrer">{t('Open PDF')}<span className="sr-only"> {t('(opens in a new tab)')}</span></a>
      </Button>
      {/* The blob page inherits this app's CSP (`object-src 'none'`), which can stop a browser's PDF viewer: download is the way that always works. */}
      <Button asChild variant="outline" className={capacityActionClass}>
        <a href={preview.url} download={evidence.filename}>{t('Download PDF')}</a>
      </Button>
    </div> : null}
  </div>
}

/** `loadKey` identifies what `load` fetches: a new key remounts the viewer and fetches again, a new `load` function alone does not. */
export function PaymentEvidencePreview({ loadKey, load, evidence }: {
  readonly loadKey: string; readonly load: EvidenceLoader; readonly evidence: Evidence
}) {
  const [attempt, setAttempt] = useState(0)
  const previewable = imageTypes.includes(evidence.content_type) || evidence.content_type === 'application/pdf'
  if (!previewable) return null
  return <EvidenceViewer key={`${loadKey}/${attempt}`} load={load} evidence={evidence} onRetry={() => setAttempt((value) => value + 1)} />
}
