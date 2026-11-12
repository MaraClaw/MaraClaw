import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { formatDistanceToNowStrict } from 'date-fns'
import { enUS, vi } from 'date-fns/locale'
import { languageStorageKey, localeTag, ownedTranslator, parseLocale, translate, type Locale, type OwnedTranslator, type Translator } from '@/lib/i18n'

type TranslationContextValue = {
  readonly locale: Locale
  readonly setLocale: (locale: Locale) => void
  readonly t: Translator
  readonly owned: OwnedTranslator
  readonly number: (value: number, options?: Intl.NumberFormatOptions) => string
  readonly date: (value: Date | string, options?: Intl.DateTimeFormatOptions) => string
  readonly distance: (value: Date | string) => string
}

const TranslationContext = createContext<TranslationContextValue | null>(null)

export function TranslationProvider({ children }: { readonly children: ReactNode }) {
  const [locale, setLocale] = useState<Locale>(() => parseLocale(localStorage.getItem(languageStorageKey)))
  useEffect(() => {
    document.documentElement.lang = locale
    localStorage.setItem(languageStorageKey, locale)
  }, [locale])
  const value = useMemo<TranslationContextValue>(() => ({
    locale, setLocale,
    t: (key, ...values) => translate(locale, key, values[0]),
    owned: ownedTranslator(locale),
    number: (amount, options) => new Intl.NumberFormat(localeTag[locale], options).format(amount),
    date: (date, options) => new Date(date).toLocaleString(localeTag[locale], options),
    distance: (date) => formatDistanceToNowStrict(new Date(date), { addSuffix: true, locale: locale === 'vi' ? vi : enUS }),
  }), [locale])
  return <TranslationContext value={value}>{children}</TranslationContext>
}

export function useTranslation(): TranslationContextValue {
  const context = useContext(TranslationContext)
  if (context === null) throw new Error('useTranslation requires TranslationProvider')
  return context
}
