import { Component, lazy, Suspense, useEffect, useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { attachmentPreviewClient, type AttachmentPreview } from '@/lib/attachment-preview-api'
import { ApiError, BinaryLimitError, userFacingRequestError } from '@/lib/http'
import { AttachmentImage } from './attachment-image'

const AttachmentPdf = lazy(() => import('./attachment-pdf').then((module) => ({ default: module.AttachmentPdf })))
const AttachmentOffice = lazy(() => import('./attachment-office').then((module) => ({ default: module.AttachmentOffice })))
type PreviewState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly preview: AttachmentPreview }
  | { readonly status: 'failed'; readonly message: string }

class RendererBoundary extends Component<{ readonly children: ReactNode }, { readonly failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() {
    return this.state.failed
      ? <p role="alert" className="text-sm text-destructive">Preview could not be displayed. Download the file to view it.</p>
      : this.props.children
  }
}

function PreviewRenderer({ preview }: { readonly preview: AttachmentPreview }) {
  switch (preview.renderer) {
    case 'image': return <AttachmentImage bytes={preview.bytes} metadata={preview.metadata} />
    case 'pdf': return <Suspense fallback={<p role="status" className="text-sm text-muted-foreground">Loading PDF viewer…</p>}>
      <AttachmentPdf bytes={preview.bytes} />
    </Suspense>
    case 'office': return <Suspense fallback={<p role="status" className="text-sm text-muted-foreground">Loading Office viewer…</p>}>
      <AttachmentOffice bytes={preview.bytes} name={preview.metadata.name} />
    </Suspense>
    case 'unsupported': return <p role="status" className="text-sm text-muted-foreground">Preview is not supported for this file type. Download it to view it.</p>
    default: return assertNever(preview)
  }
}

function assertNever(value: never): never { throw new TypeError(`Unexpected attachment preview: ${String(value)}`) }

export function AttachmentPreviewBody({ agentId, path }: { readonly agentId: string; readonly path: string }) {
  const [attempt, setAttempt] = useState(0)
  return <PreviewAttempt key={attempt} agentId={agentId} path={path} onRetry={() => setAttempt((value) => value + 1)} />
}

function PreviewAttempt({ agentId, path, onRetry }: {
  readonly agentId: string; readonly path: string; readonly onRetry: () => void
}) {
  const scope = useWorkspaceSession()
  const [state, setState] = useState<PreviewState>({ status: 'loading' })
  useEffect(() => {
    const controller = new AbortController()
    void attachmentPreviewClient.open(scope, { agentId, path }, controller.signal).then(
      (preview) => { if (!controller.signal.aborted && scope.isCurrent()) setState({ status: 'ready', preview }) },
      (error: unknown) => {
        if (controller.signal.aborted || !scope.isCurrent()) return
        const message = error instanceof BinaryLimitError || (error instanceof ApiError && error.status === 413)
          ? new BinaryLimitError().message
          : userFacingRequestError(error, 'Preview is unavailable. Try again or download the file.')
        setState({ status: 'failed', message })
      },
    )
    return () => controller.abort()
  }, [scope, agentId, path])

  switch (state.status) {
    case 'loading': return <p role="status" className="text-sm text-muted-foreground">Checking access and loading preview…</p>
    case 'failed': return <div className="space-y-4">
      <p role="alert" className="text-sm text-destructive">{state.message}</p>
      <Button type="button" variant="outline" className="min-h-11" onClick={onRetry}>Retry preview</Button>
    </div>
    case 'ready': return <RendererBoundary><PreviewRenderer preview={state.preview} /></RendererBoundary>
    default: return assertNever(state)
  }
}
