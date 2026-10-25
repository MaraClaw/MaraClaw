import { z } from 'zod'

export const SSO_STATE_KEY = 'maraclaw-sso-state'
export const SSO_PROVIDER_KEY = 'maraclaw-sso-provider'
export const SSO_INTENT_KEY = 'maraclaw-sso-intent'
export const SSO_CONTINUATION_KEY = 'maraclaw-sso-continuation'

type SsoTransaction = {
  readonly state: string | null
  readonly provider: string | null
  readonly intent?: string | null
}

const invitationPath = z.string().max(2048).refine((path) => {
  const code = new URLSearchParams(path.split('#')[1]).get('code')
  return Boolean(code && code.length <= 256 && path === `/transfer#code=${encodeURIComponent(code)}`)
}).brand<'InvitationContinuation'>()
const routerContinuation = z.object({ from: z.object({ pathname: invitationPath }) })
const storedContinuation = z.object({ state: z.string().min(1), provider: z.string().min(1), path: invitationPath }).readonly()

export function storeSsoTransaction(transaction: { readonly state: string; readonly provider: string }, routerState: unknown): void {
  sessionStorage.setItem(SSO_STATE_KEY, transaction.state)
  sessionStorage.setItem(SSO_PROVIDER_KEY, transaction.provider)
  sessionStorage.removeItem(SSO_INTENT_KEY)
  sessionStorage.removeItem(SSO_CONTINUATION_KEY)
  const parsed = routerContinuation.safeParse(routerState)
  if (parsed.success) {
    sessionStorage.setItem(SSO_CONTINUATION_KEY, JSON.stringify({ ...transaction, path: parsed.data.from.pathname }))
  }
}

export function readSsoContinuation(transaction: SsoTransaction, callback: SsoTransaction): z.infer<typeof invitationPath> | null {
  if (transaction.intent === 'bind' || !transaction.state || transaction.state !== callback.state || transaction.provider !== callback.provider) return null
  const stored = sessionStorage.getItem(SSO_CONTINUATION_KEY)
  if (!stored) return null
  try {
    const value: unknown = JSON.parse(stored)
    const parsed = storedContinuation.safeParse(value)
    return parsed.success && parsed.data.state === transaction.state && parsed.data.provider === transaction.provider
      ? parsed.data.path
      : null
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error
    return null
  }
}

export function consumeSsoTransaction(transaction: SsoTransaction): void {
  if (sessionStorage.getItem(SSO_STATE_KEY) !== transaction.state || sessionStorage.getItem(SSO_PROVIDER_KEY) !== transaction.provider) return
  sessionStorage.removeItem(SSO_STATE_KEY)
  sessionStorage.removeItem(SSO_PROVIDER_KEY)
  sessionStorage.removeItem(SSO_INTENT_KEY)
  sessionStorage.removeItem(SSO_CONTINUATION_KEY)
}
