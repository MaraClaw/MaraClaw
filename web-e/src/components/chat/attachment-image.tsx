import { useEffect, useRef, useState } from 'react'
import type { AttachmentMetadata } from '@/lib/attachment-preview-api'

export function AttachmentImage({ bytes, metadata }: {
  readonly bytes: Uint8Array
  readonly metadata: AttachmentMetadata
}) {
  const image = useRef<HTMLImageElement>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'failed'>('loading')
  useEffect(() => {
    const element = image.current
    if (!element) return
    const objectUrl = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: metadata.content_type }))
    element.src = objectUrl
    return () => { element.removeAttribute('src'); URL.revokeObjectURL(objectUrl) }
  }, [bytes, metadata.content_type])

  return <div className="flex min-h-40 flex-col items-center gap-3">
    {status === 'loading' ? <p role="status" className="text-sm text-muted-foreground">Loading image…</p> : null}
    {status === 'failed' ? <p role="alert" className="text-sm text-destructive">This image could not be displayed. Download it to view it.</p> : null}
    <img ref={image} hidden={status === 'failed'} alt={metadata.name} className="max-h-[65dvh] max-w-full rounded-xl object-contain"
      onLoad={() => setStatus('ready')} onError={() => setStatus('failed')} />
  </div>
}
