import { useEffect } from 'react'

import { useSessionCurrent } from '@/hooks/use-auth'
import { useTranslation } from '@/hooks/use-translation'
import { requestFailureMessage, showErrorToast } from '@/lib/error-toast'
import { translate } from '@/lib/i18n'

export function ErrorToast({ message, error }: {
  readonly message: string
  readonly error?: unknown
}) {
  const isCurrent = useSessionCurrent()
  const { locale } = useTranslation()
  const presented = requestFailureMessage(error, message)
  const text = typeof presented === 'string' ? presented : translate(locale, presented.key, presented.values)
  useEffect(() => {
    if (isCurrent()) showErrorToast(text, error)
  }, [text, error, isCurrent])
  return null
}
