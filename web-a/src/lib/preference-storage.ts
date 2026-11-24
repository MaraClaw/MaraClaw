/**
 * Optional UI preferences (theme, language) in `localStorage`. A browser that refuses storage throws a
 * `DOMException` (`SecurityError` even from the `localStorage` getter when site data is blocked,
 * `QuotaExceededError` on a full write); the preference then lives only in memory for this page.
 * Not for the admin token: `auth-storage.ts` owns that policy.
 */
export function readPreference(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch (error) {
    if (error instanceof DOMException) return null
    throw error
  }
}

export function writePreference(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch (error) {
    if (error instanceof DOMException) return
    throw error
  }
}
