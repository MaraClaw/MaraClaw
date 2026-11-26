import type { MessageKey, vietnamese } from './translations.ts'
import { retryingImport } from './retrying-import.ts'

export type Locale = 'en' | 'vi'
export const languageStorageKey = 'maraclaw-admin-language'
export const localeTag = { en: 'en-US', vi: 'vi-VN' } as const
/** Endonyms: each language is always named in itself, whatever the active locale. */
export const languageName = { en: 'English', vi: 'Tiếng Việt' } as const satisfies Record<Locale, string>
// The dictionary is type-only here: its strings are their own chunk, fetched by `loadVietnamese`.
let dictionary: typeof vietnamese | null = null

/** Fetches the Vietnamese strings once. Rejects (and lets a later call retry) when the chunk cannot load. */
export const loadVietnamese: () => Promise<void> = retryingImport(() => import('./translations.ts').then((module) => { dictionary = module.vietnamese }))

export type MessageValues = Readonly<Record<string, string | number>>
export type LocalMessage = { readonly key: MessageKey; readonly values: MessageValues }
export function message(key: MessageKey, values: MessageValues = {}): LocalMessage {
  return { key, values }
}
type Slots<S extends string> = S extends `${string}{${infer Key}}${infer Rest}` ? Key | Slots<Rest> : never
export type Translator = <Key extends MessageKey>(key: Key, ...values: [Slots<Key>] extends [never]
  ? [values?: MessageValues] : [values: Readonly<Record<Slots<Key>, string | number>>]) => string

export function parseLocale(value: string | null): Locale {
  return value === 'vi' ? 'vi' : 'en'
}

export function translate(locale: Locale, key: MessageKey, values: MessageValues = {}): string {
  // Until `loadVietnamese` resolves, `vi` renders the English key.
  const message = locale === 'vi' && dictionary !== null ? dictionary[key] : key
  return message.replace(/\{([^{}]+)\}/g, (slot: string, name: string) =>
    Object.hasOwn(values, name) ? String(values[name]) : slot)
}

/** Only for client-owned schema/presentation helpers. Never for API detail or user content. */
export function translateOwned(locale: Locale, message: string | undefined): string | undefined {
  if (message === undefined) return undefined
  return isMessageKey(message) ? translate(locale, message) : message
}

export type OwnedTranslator = { (message: string): string; (message: string | undefined): string | undefined }
export function ownedTranslator(locale: Locale): OwnedTranslator {
  function owned(message: string): string
  function owned(message: string | undefined): string | undefined
  function owned(message: string | undefined) { return translateOwned(locale, message) }
  return owned
}

function isMessageKey(message: string): message is MessageKey {
  return dictionary !== null && Object.hasOwn(dictionary, message)
}
