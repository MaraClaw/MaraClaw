import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { TranslationContext } from '@/hooks/use-translation'
import { createTranslator, loadVietnamese, localeTags, persistLocale, readLocale, translateOwned, type Locale, type Translate } from '@/lib/i18n/translation'

/** Renders nothing while a stored `vi` locale waits for its strings, so the first frame is never English. */
export function TranslationProvider({ children }: { readonly children: ReactNode }) {
  const [locale, updateLocale] = useState<Locale>(() => readLocale())
  const [ready, setReady] = useState(locale === 'en')
  const currentLocale = useRef(locale)
  const latestSwitch = useRef(0)
  const t = useCallback<Translate>((key, ...values) => createTranslator(currentLocale.current)(key, ...values), [])
  const owned = useCallback((key: string) => translateOwned(locale, key), [locale])
  const value = useMemo(() => ({
    locale,
    t,
    owned,
    setLocale(next: Locale) {
      const request = ++latestSwitch.current
      const apply = () => {
        currentLocale.current = next
        persistLocale(next)
        updateLocale(next)
      }
      if (next === 'en') return apply()
      // A failed load keeps the current language; the selector stays usable and a later switch retries.
      loadVietnamese().then(() => { if (request === latestSwitch.current) apply() }, () => undefined)
    },
    formatDate(input: string | number | Date, options?: Intl.DateTimeFormatOptions) {
      return new Intl.DateTimeFormat(localeTags[locale], options).format(new Date(input))
    },
    formatNumber(input: number, options?: Intl.NumberFormatOptions) {
      return new Intl.NumberFormat(localeTags[locale], options).format(input)
    },
  }), [locale, owned, t])
  useEffect(() => {
    if (ready) return
    loadVietnamese().then(undefined, () => {
      // The stored preference stays `vi` so the next visit retries; this page falls back to English.
      currentLocale.current = 'en'
      updateLocale('en')
    }).then(() => setReady(true))
  }, [ready])
  useEffect(() => {
    document.documentElement.lang = locale
  }, [locale])
  return <TranslationContext value={value}>{ready ? children : null}</TranslationContext>
}
