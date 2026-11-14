import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { useTranslation } from '@/hooks/use-translation'
import { fetchCapacityEvidence, type CapacityOrder, type CapacityOrderId } from '@/lib/capacity-api'
import { ApiError, BinaryLimitError } from '@/lib/http'
import { capacityActionClass } from './capacity-labels'

type Evidence = NonNullable<CapacityOrder['evidence']>
type Preview =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly url: string }
  | { readonly status: 'error'; readonly error: unknown }

const imageTypes: readonly string[] = ['image/png', 'image/jpeg', 'image/webp']

function EvidenceViewer({ orderId, evidence, onRetry }: {
  readonly orderId: CapacityOrderId; readonly evidence: Evidence; readonly onRetry: () => void
}) {
  const { t } = useTranslation()
  const [preview, setPreview] = useState<Preview>({ status: 'loading' })
  const isImage = imageTypes.includes(evidence.content_type)

  useEffect(() => {
    const controller = new AbortController()
    let objectUrl: string | null = null
    fetchCapacityEvidence(orderId, controller.signal).then((bytes) => {
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
  }, [orderId, evidence.content_type])

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

export function CapacityEvidencePreview({ orderId, evidence }: { readonly orderId: CapacityOrderId; readonly evidence: Evidence }) {
  const [attempt, setAttempt] = useState(0)
  const previewable = imageTypes.includes(evidence.content_type) || evidence.content_type === 'application/pdf'
  if (!previewable) return null
  return <EvidenceViewer key={attempt} orderId={orderId} evidence={evidence} onRetry={() => setAttempt((value) => value + 1)} />
}
