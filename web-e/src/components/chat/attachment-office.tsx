import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { loadOfficeViewer, officeFormat, officePreviewError } from '@/lib/office-preview'
import { createOfficeViewer, type NavigableOfficeViewer } from './office-viewer'
import './attachment-office.css'

type OfficeState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly index: number; readonly count: number; readonly label: string }
  | { readonly status: 'failed'; readonly message: string }

export function AttachmentOffice({ bytes, name }: { readonly bytes: Uint8Array; readonly name: string }) {
  const hostRef = useRef<HTMLElement>(null)
  const viewerRef = useRef<NavigableOfficeViewer | null>(null)
  const lifetimeRef = useRef<AbortController | null>(null)
  const [state, setState] = useState<OfficeState>({ status: 'loading' })
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const host = hostRef.current
    const format = officeFormat(name)
    if (!host || !format) return
    const controller = new AbortController()
    lifetimeRef.current = controller
    let observer: ResizeObserver | undefined
    let loaded = false
    const update = () => {
      const viewer = viewerRef.current
      if (viewer && !controller.signal.aborted) setState({ status: 'ready', index: viewer.index, count: viewer.count, label: viewer.label })
    }
    const fail = (error: unknown) => {
      if (controller.signal.aborted) return
      setState({ status: 'failed', message: officePreviewError(error) })
      controller.abort()
    }
    void createOfficeViewer(host, { format, signal: controller.signal, onChange: () => { if (loaded) update() }, onError: fail }).then(async (viewer) => {
      if (controller.signal.aborted) { viewer.destroy(); return }
      viewerRef.current = viewer
      await loadOfficeViewer(viewer, bytes, controller.signal)
      if (controller.signal.aborted) return
      loaded = true
      update()
      observer = new ResizeObserver(() => { void Promise.resolve(viewer.fit()).catch(fail) })
      observer.observe(host)
    }).catch(fail)
    return () => {
      controller.abort()
      observer?.disconnect()
      viewerRef.current = null
      lifetimeRef.current = null
      host.replaceChildren()
    }
  }, [bytes, name])

  const navigate = async (index: number) => {
    const viewer = viewerRef.current
    const lifetime = lifetimeRef.current
    if (!viewer || !lifetime || busy || lifetime.signal.aborted) return
    setBusy(true)
    try {
      await viewer.goTo(index)
      if (!lifetime.signal.aborted) setState({ status: 'ready', index: viewer.index, count: viewer.count, label: viewer.label })
    } catch (error) {
      if (!lifetime.signal.aborted) { setState({ status: 'failed', message: officePreviewError(error) }); lifetime.abort() }
    } finally {
      if (!lifetime.signal.aborted) setBusy(false)
    }
  }

  return <div className="space-y-4">
    {state.status === 'loading' && <p role="status" className="text-sm text-muted-foreground">Loading Office preview…</p>}
    {state.status === 'failed' && <p role="alert" className="text-sm text-destructive">{state.message}</p>}
    <section ref={hostRef} aria-label={`${name} document preview`} hidden={state.status === 'failed'}
      className={`office-preview min-w-0 overflow-hidden rounded-xl bg-white text-black ${officeFormat(name) === 'xlsx' ? 'h-[60dvh] min-h-64' : ''}`} />
    {state.status === 'ready' && <nav aria-label="Document pages" className="flex flex-wrap items-center justify-center gap-3">
      <Button type="button" variant="outline" className="min-h-11" disabled={busy || state.index <= 0} onClick={() => void navigate(state.index - 1)}>Previous</Button>
      <p role="status" aria-live="polite" className="text-sm text-muted-foreground">{state.label} {state.index + 1} of {state.count}</p>
      <Button type="button" variant="outline" className="min-h-11" disabled={busy || state.index + 1 >= state.count} onClick={() => void navigate(state.index + 1)}>Next</Button>
    </nav>}
  </div>
}
