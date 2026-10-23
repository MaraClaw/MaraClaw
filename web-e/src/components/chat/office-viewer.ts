import { officePreviewOptions, type OfficeFormat, type OfficeViewer } from '@/lib/office-preview'

export type NavigableOfficeViewer = OfficeViewer & {
  readonly index: number
  readonly count: number
  readonly label: string
  readonly goTo: (index: number) => Promise<void>
  readonly fit: () => Promise<void> | void
}
type OfficeMount = {
  readonly format: OfficeFormat
  readonly signal: AbortSignal
  readonly onChange: () => void
  readonly onError: (error: Error) => void
}

export async function createOfficeViewer(host: HTMLElement, mount: OfficeMount): Promise<NavigableOfficeViewer> {
  const options = { ...officePreviewOptions, onError: mount.onError }
  switch (mount.format) {
    case 'docx': {
      const { DocxViewer } = await import('@silurus/ooxml/docx')
      mount.signal.throwIfAborted()
      const canvas = host.appendChild(document.createElement('canvas'))
      const viewer = new DocxViewer(canvas, { ...options, enableTextSelection: true, onPageChange: mount.onChange })
      return {
        load: async (source) => { await viewer.load(source); await viewer.waitUntilLayoutComplete(); await viewer.fitWidth() },
        destroy: () => viewer.destroy(), fit: () => viewer.fitWidth(), goTo: (index) => viewer.goToPage(index),
        get index() { return viewer.currentPage }, get count() { return viewer.pageCount }, get label() { return 'Page' },
      }
    }
    case 'pptx': {
      const { PptxViewer } = await import('@silurus/ooxml/pptx')
      mount.signal.throwIfAborted()
      const canvas = host.appendChild(document.createElement('canvas'))
      const viewer = new PptxViewer(canvas, {
        ...options, enableTextSelection: true, enableMediaPlayback: false, onSlideChange: mount.onChange,
      })
      return {
        load: async (source) => { await viewer.load(source); await viewer.waitUntilLayoutComplete(); await viewer.fitWidth() },
        destroy: () => viewer.destroy(), fit: () => viewer.fitWidth(), goTo: (index) => viewer.goToSlide(index),
        get index() { return viewer.slideIndex }, get count() { return viewer.slideCount }, get label() { return 'Slide' },
      }
    }
    case 'xlsx': {
      const { XlsxViewer } = await import('@silurus/ooxml/xlsx')
      mount.signal.throwIfAborted()
      const viewer = new XlsxViewer(host, {
        ...options, showZoomSlider: false, resizable: false, onSheetChange: mount.onChange,
        xlsxWorksheetLimits: { maxRows: 100_000, maxCells: 250_000, maxOwnedUtf8Bytes: 32 * 1024 * 1024, maxJsonBytes: 64 * 1024 * 1024 },
      })
      return {
        load: (source) => viewer.load(source), destroy: () => viewer.destroy(), fit: () => viewer.relayout(),
        goTo: (index) => viewer.goToSheet(index),
        get index() { return viewer.sheetIndex }, get count() { return viewer.sheetCount }, get label() { return 'Sheet' },
      }
    }
    default: return assertNever(mount.format)
  }
}

function assertNever(value: never): never { throw new TypeError(`Unexpected Office format: ${String(value)}`) }
