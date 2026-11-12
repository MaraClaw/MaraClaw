import { useTranslation } from '@/hooks/use-translation'
import type { TranslationKey } from '@/lib/i18n/catalog'

export function Translation({ id }: { readonly id: TranslationKey }) {
  const { owned } = useTranslation()
  return owned(id)
}
