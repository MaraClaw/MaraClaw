import { useEffect, useMemo, useRef, useState } from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/Page/TextLayer.css'
import { Button } from '@/components/ui/button'

pdfjs.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString()
const options = { isEvalSupported: false, useWasm: false, maxImageSize: 16_777_216 }
const failure = <p role="alert" className="text-sm text-destructive">This PDF could not be displayed. Download it to view it.</p>

export function AttachmentPdf({ bytes }: { readonly bytes: Uint8Array }) {
  // PDF.js transfers its input buffer to the worker; never give it the LRU's buffer.
  const file = useMemo(() => ({ data: new Uint8Array(bytes) }), [bytes])
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [width, setWidth] = useState(280)
  const [locked, setLocked] = useState(false)
  const container = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const element = container.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.max(1, Math.min(1000, entry.contentRect.width)))
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return <div ref={container} className="min-w-0">
    {locked ? <p role="alert" className="text-sm text-muted-foreground">This PDF is password-protected. Download it to view it.</p> : null}
    {pages ? <nav aria-label="PDF pages" className="mb-4 flex flex-wrap items-center justify-center gap-3">
      <Button type="button" variant="outline" size="sm" className="min-h-11" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>Previous page</Button>
      <p role="status" aria-atomic="true" className="text-xs text-muted-foreground">Page {page} of {pages}</p>
      <Button type="button" variant="outline" size="sm" className="min-h-11" disabled={page >= pages} onClick={() => setPage((value) => value + 1)}>Next page</Button>
    </nav> : null}
    <Document file={file} options={options} suspense={false} error={failure}
      loading={<p role="status" className="text-sm text-muted-foreground">Loading PDF…</p>}
      onPassword={() => setLocked(true)} onLoadSuccess={({ numPages }) => setPages(numPages)}>
      <Page key={page} pageNumber={page} width={width} devicePixelRatio={Math.min(window.devicePixelRatio || 1, 2)}
        renderAnnotationLayer={false} renderTextLayer error={failure}
        loading={<p role="status" className="text-sm text-muted-foreground">Loading page {page}…</p>} />
    </Document>
  </div>
}
