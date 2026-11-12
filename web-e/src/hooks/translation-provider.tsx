import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { TranslationContext } from '@/hooks/use-translation'
import { createTranslator, localeTags, persistLocale, readLocale, translateOwned, type Locale, type Translate } from '@/lib/i18n/translation'

export function TranslationProvider({ children }: { readonly children: ReactNode }) {
  const [locale, updateLocale] = useState<Locale>(() => readLocale())
  const currentLocale = useRef(locale)
  const t = useCallback<Translate>((key, ...values) => createTranslator(currentLocale.current)(key, ...values), [])
  const owned = useCallback((key: string) => translateOwned(locale, key), [locale])
  const value = useMemo(() => ({
    locale,
    t,
    owned,
    setLocale(next: Locale) {
      currentLocale.current = next
      persistLocale(next)
      updateLocale(next)
    },
    formatDate(input: string | number | Date, options?: Intl.DateTimeFormatOptions) {
      return new Intl.DateTimeFormat(localeTags[locale], options).format(new Date(input))
    },
    formatNumber(input: number, options?: Intl.NumberFormatOptions) {
      return new Intl.NumberFormat(localeTags[locale], options).format(input)
    },
  }), [locale, owned, t])
  useEffect(() => {
    document.documentElement.lang = locale
  }, [locale])
  return <TranslationContext value={value}>{children}</TranslationContext>
}
