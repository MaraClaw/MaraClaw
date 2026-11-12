import { useTranslation } from '@/hooks/use-translation'

export function ValidationMessage({ message }: { readonly message: string | undefined }) {
  const { owned } = useTranslation()
  return message ? owned(message) : null
}
