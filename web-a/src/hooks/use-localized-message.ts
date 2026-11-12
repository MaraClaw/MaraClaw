import { useState } from 'react'
import { useTranslation } from './use-translation'
import { translate, type LocalMessage } from '@/lib/i18n'

export function useLocalizedMessage() {
  const [value, setValue] = useState<string | LocalMessage | null>(null)
  const { locale } = useTranslation()
  return [typeof value === 'object' && value !== null ? translate(locale, value.key, value.values) : value, setValue] as const
}
