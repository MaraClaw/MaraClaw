import { vietnamese, type TranslationKey } from './catalog.ts'

export type Locale = 'en' | 'vi'
export const LANGUAGE_STORAGE_KEY = 'maraclaw-language'
export const localeTags = { en: 'en-US', vi: 'vi-VN' } as const

export function isTranslationKey(value: string): value is TranslationKey {
  return Object.hasOwn(vietnamese, value)
}

export function parseLocale(value: unknown): Locale {
  return value === 'vi' ? 'vi' : 'en'
}

type ParametersOf<Key extends string> = Key extends `${string}{${infer Name}}${infer Rest}`
  ? Name | ParametersOf<Rest>
  : never
type Values<Key extends TranslationKey> = [ParametersOf<Key>] extends [never]
  ? readonly [values?: never]
  : readonly [values: Readonly<Record<ParametersOf<Key>, string | number>>]

export type Translate = <Key extends TranslationKey>(key: Key, ...values: Values<Key>) => string

export function translateOwned(locale: Locale, key: string): string {
  return locale === 'vi' && isTranslationKey(key) ? vietnamese[key] : key
}

export function createTranslator(locale: Locale): Translate {
  return (key, ...[values]) => {
    const text = translateOwned(locale, key)
    return text.replace(/\{(\w+)\}/g, (placeholder: string, name: string) => {
      const value = values && Object.entries(values).find(([key]) => key === name)?.[1]
      return value === undefined ? placeholder : String(value)
    })
  }
}

export function readLocale(storage?: Pick<Storage, 'getItem'>): Locale {
  try {
    return parseLocale((storage ?? window.localStorage).getItem(LANGUAGE_STORAGE_KEY))
  } catch {
    return 'en'
  }
}

export function persistLocale(locale: Locale, storage?: Pick<Storage, 'setItem'>): void {
  try {
    ;(storage ?? window.localStorage).setItem(LANGUAGE_STORAGE_KEY, locale)
  } catch {
    // Restricted browser storage must not prevent an in-memory language switch.
  }
}
