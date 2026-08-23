/**
 * Public API base for the landing / member app.
 * Empty string = same-origin (Vite proxy in dev: /api → engine).
 */
export function getApiBaseUrl(): string {
  const raw = import.meta.env.VITE_API_BASE_URL
  if (typeof raw === 'string' && raw.trim()) {
    const base = raw.replace(/\/$/, '')
    try {
      if (new URL(base).hostname === '0.0.0.0') {
        return ''
      }
    } catch {
      // Relative or invalid - keep the trimmed value.
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

export function wsUrl(path: string): string {
  const normalized = path.startsWith('/') ? path : `/${path}`
  const base = getApiBaseUrl()
  if (!base) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${window.location.host}${normalized}`
  }
  try {
    const url = new URL(base)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    url.pathname = normalized
    url.search = ''
    url.hash = ''
    return url.toString()
  } catch {
    return normalized
  }
}
