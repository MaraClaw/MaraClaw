import { useTranslation } from '@/hooks/use-translation'
import { useEffect, useRef, useState } from 'react'
import type { AttachmentMetadata } from '@/lib/attachment-preview-api'

export function AttachmentVideo({ bytes, metadata }: {
  readonly bytes: Uint8Array
  readonly metadata: AttachmentMetadata
}) {
  const { t: translate } = useTranslation()
  const video = useRef<HTMLVideoElement>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    const element = video.current
    if (!element) return
    const objectUrl = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: metadata.content_type }))
    element.src = objectUrl
    return () => {
      element.pause()
      element.removeAttribute('src')
      element.load()
      URL.revokeObjectURL(objectUrl)
    }
  }, [bytes, metadata.content_type])

  return <div className="flex min-h-40 flex-col items-center gap-3">
    {failed ? <p role="alert" className="text-sm text-destructive">{translate("Preview could not be displayed. Download the file to view it.")}</p> : null}
    <video ref={video} hidden={failed} aria-label={metadata.name} controls preload="none" playsInline
      width={640} height={360} className="max-h-[65dvh] max-w-full rounded-xl object-contain" onError={() => setFailed(true)} />
  </div>
}
