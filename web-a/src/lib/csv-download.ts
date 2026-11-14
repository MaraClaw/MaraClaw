const REVOKE_DELAY_MS = 10_000

/** Saves CSV text already held in memory as a file; the object URL is released after the browser has picked it up. */
export function downloadCsvFile(csv: string, filename: string) {
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  const anchor = document.createElement('a')
  try {
    anchor.href = url
    anchor.download = filename
    document.body.append(anchor)
    anchor.click()
  } finally {
    anchor.remove()
    setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS)
  }
}
