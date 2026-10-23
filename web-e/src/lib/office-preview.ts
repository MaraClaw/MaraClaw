export type OfficeFormat = 'docx' | 'xlsx' | 'pptx'

export function officeFormat(name: string): OfficeFormat | undefined {
  const extension = name.split('.').at(-1)?.toLowerCase()
  switch (extension) {
    case 'docx': case 'xlsx': case 'pptx': return extension
    default: return undefined
  }
}

export const officePreviewOptions = Object.freeze({
  mode: 'worker' as const,
  useGoogleFonts: false,
  enableHyperlinks: false,
  workerTimeoutMs: 10_000,
  resourceLimits: Object.freeze({
    maxArchiveEntryBytes: 32 * 1024 * 1024,
    maxTotalInflatedBytes: 64 * 1024 * 1024,
    maxArchiveEntries: 2000,
  }),
  imageResources: Object.freeze({ decodedByteBudget: 32 * 1024 * 1024 }),
})

export type OfficeViewer = {
  readonly load: (source: ArrayBuffer) => Promise<void>
  readonly destroy: () => void
}

export class OfficePreviewTimeoutError extends Error {
  constructor() { super('Office preview took too long. Download the file to view it.'); this.name = 'OfficePreviewTimeoutError' }
}

export function officePreviewError(error: unknown): string {
  if (error instanceof OfficePreviewTimeoutError) return error.message
  if (error instanceof Error && 'code' in error && error.code === 'ooxml-resource-limit') {
    return 'This document exceeds preview processing limits. Download it to view it.'
  }
  return 'This document could not be previewed. It may be damaged or unsupported. Download it to view it.'
}

export async function loadOfficeViewer(viewer: OfficeViewer, bytes: Uint8Array, signal: AbortSignal): Promise<void> {
  let destroyed = false
  const destroy = () => { if (!destroyed) { destroyed = true; viewer.destroy() } }
  let timer: ReturnType<typeof setTimeout> | undefined
  let onAbort = () => destroy()
  const interrupted = new Promise<never>((_resolve, reject) => {
    onAbort = () => { destroy(); reject(signal.reason) }
    signal.addEventListener('abort', onAbort, { once: true })
    timer = setTimeout(() => { destroy(); reject(new OfficePreviewTimeoutError()) }, 20_000)
  })
  try {
    signal.throwIfAborted()
    // The parser transfers its input. Never detach the owner-scoped LRU buffer.
    await Promise.race([viewer.load(bytes.slice().buffer), interrupted])
    signal.throwIfAborted()
  } catch (error) {
    destroy()
    signal.removeEventListener('abort', onAbort)
    throw error
  } finally {
    clearTimeout(timer)
  }
}
