/**
 * Public API base for the admin console.
 * Empty string = same-origin (Vite proxy in dev: /api → engine).
 */
export function getApiBaseUrl(): string {
  const raw = import.meta.env.VITE_API_BASE_URL
  if (typeof raw === 'string' && raw.trim()) {
    const base = raw.replace(/\/$/, '')
    try {
      // 0.0.0.0 is a listen address. Browsers cannot use it as a destination.
      if (new URL(base).hostname === '0.0.0.0') {
        return ''
      }
    } catch {
      // Relative or invalid — keep the trimmed value.
    }
    return base
  }
  return ''
}

export function apiUrl(path: string): string {
  const base = getApiBaseUrl()
  const normalized = path.startsWith('/') ? path : `/${path}`
  return `${base}${normalized}`
}
