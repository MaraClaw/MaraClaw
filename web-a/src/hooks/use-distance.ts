import { formatDistanceToNowStrict } from 'date-fns'
import { enUS, vi } from 'date-fns/locale'
import { useCallback } from 'react'

import { useTranslation } from '@/hooks/use-translation'

/** Relative time ("3 days ago") in the active language. Kept apart from `useTranslation` so date-fns stays out of the entry chunk. */
export function useDistance(): (value: Date | string) => string {
  const { locale } = useTranslation()
  return useCallback(
    (value) => formatDistanceToNowStrict(new Date(value), { addSuffix: true, locale: locale === 'vi' ? vi : enUS }),
    [locale],
  )
}
