import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { languageStorageKey, loadVietnamese, localeTag, ownedTranslator, parseLocale, translate, type Locale, type OwnedTranslator, type Translator } from '@/lib/i18n'
import { readPreference, writePreference } from '@/lib/preference-storage'

type TranslationContextValue = {
  readonly locale: Locale
  readonly setLocale: (locale: Locale) => void
  readonly t: Translator
  readonly owned: OwnedTranslator
  readonly number: (value: number, options?: Intl.NumberFormatOptions) => string
  readonly date: (value: Date | string, options?: Intl.DateTimeFormatOptions) => string
}

const TranslationContext = createContext<TranslationContextValue | null>(null)

/** Renders nothing while a stored `vi` locale waits for its strings, so the first frame is never English. */
export function TranslationProvider({ children }: { readonly children: ReactNode }) {
  const [locale, updateLocale] = useState<Locale>(() => parseLocale(readPreference(languageStorageKey)))
  const [ready, setReady] = useState(locale === 'en')
  const latestSwitch = useRef(0)
  const setLocale = useCallback((next: Locale) => {
    const request = ++latestSwitch.current
    const apply = () => {
      writePreference(languageStorageKey, next)
      updateLocale(next)
    }
    if (next === 'en') return apply()
    // A failed load keeps the current language; the toggle stays usable and a later switch retries.
    loadVietnamese().then(() => { if (request === latestSwitch.current) apply() }, () => undefined)
  }, [])
  useEffect(() => {
    if (ready) return
    loadVietnamese().then(undefined, () => {
      // The stored preference stays `vi` so the next visit retries; this page falls back to English.
      updateLocale('en')
    }).then(() => setReady(true))
  }, [ready])
  useEffect(() => {
    document.documentElement.lang = locale
  }, [locale])
  const value = useMemo<TranslationContextValue>(() => ({
    locale, setLocale,
    t: (key, ...values) => translate(locale, key, values[0]),
    owned: ownedTranslator(locale),
    number: (amount, options) => new Intl.NumberFormat(localeTag[locale], options).format(amount),
    date: (date, options) => new Date(date).toLocaleString(localeTag[locale], options),
  }), [locale, setLocale])
  return <TranslationContext value={value}>{ready ? children : null}</TranslationContext>
}

export function useTranslation(): TranslationContextValue {
  const context = useContext(TranslationContext)
  if (context === null) throw new Error('useTranslation requires TranslationProvider')
  return context
}
