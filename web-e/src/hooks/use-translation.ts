import { createContext, useContext } from 'react'
import type { Locale, Translate } from '@/lib/i18n/translation'

export type TranslationContextValue = {
  readonly locale: Locale
  readonly setLocale: (locale: Locale) => void
  readonly t: Translate
  readonly owned: (key: string) => string
  readonly formatDate: (value: string | number | Date, options?: Intl.DateTimeFormatOptions) => string
  readonly formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string
}

export const TranslationContext = createContext<TranslationContextValue | null>(null)

export function useTranslation(): TranslationContextValue {
  const context = useContext(TranslationContext)
  if (!context) throw new Error('useTranslation requires TranslationProvider')
  return context
}
