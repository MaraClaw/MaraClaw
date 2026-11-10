import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { QRCodeSVG } from 'qrcode.react'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Button } from '@/components/ui/button'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { changePersonalConnection, getPersonalStatus, type PersonalStatus } from '@/lib/whatsapp-api'
import { createPersonalSession } from '@/lib/whatsapp-personal-session'

export function WhatsAppPersonalCard({ agentId }: { readonly agentId: string }) {
  const scope = useWorkspaceSession()
  const session = useMemo(() => createPersonalSession({
    current: scope.isCurrent,
    read: (signal) => getPersonalStatus(agentId, { owner: scope.owner, signal }),
    change: (command, signal) => changePersonalConnection(agentId, command, { owner: scope.owner, signal }),
  }), [agentId, scope])
  const view = useSyncExternalStore(session.subscribe, session.getSnapshot)
  useEffect(() => { session.start(); return session.stop }, [session])
  const status = view.status
  const generation = status?.generation ?? null
  const busy = view.busy || view.loading
  const qr = status?.qr
  const canPair = status && ['disconnected', 'expired', 'failed', 'paused', 'connected', 'reconnecting'].includes(status.state)
  return <section aria-label="Personal QR connection" className="min-w-0 space-y-3">
    <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">Unofficial WhatsApp automation may be restricted or suspend your account. Use an account you own and accept this risk before connecting.</p>
    <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">First version: newly authored text in your own self-chat only. No contacts, groups, media, history, edits, or proactive messages. One WhatsApp mode and account can be active per agent; connecting here replaces Cloud setup.</p>
    <p role="status" aria-atomic="true" className="text-lg sm:text-sm">{view.loading ? 'Loading personal connection…' : view.busy ? 'Updating personal connection…' : status ? stateText(status.state) : ''}</p>
    {view.error ? <RequestErrorToast message={view.error} /> : null}
    {status?.account_hint ? <p className="break-all text-lg sm:text-sm">Account: {status.account_hint}</p> : null}
    {status?.error_code ? <p className="max-w-prose text-lg text-destructive sm:text-sm">{failureText(status.error_code)}</p> : null}
    {qr ? <div className="space-y-2">
      <QRCodeSVG value={qr.value} size={240} marginSize={4} level="M" role="img" title="WhatsApp pairing QR" aria-label="WhatsApp pairing QR" className="h-auto max-w-full" />
      <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">In WhatsApp on your phone, open Linked devices, then Link a device and scan this code. Do not share it.</p>
      <p className="text-base text-muted-foreground sm:text-sm">Expires at {new Date(qr.expires_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}. An expired code is removed automatically.</p>
    </div> : null}
    <div className="flex flex-wrap gap-3">
      {canPair ? <Button type="button" className="border border-transparent text-base disabled:border-muted-foreground disabled:bg-muted disabled:text-foreground disabled:opacity-100 disabled:shadow-none sm:text-sm" disabled={busy} onClick={scope.effect(() => void session.change(generation === null ? { kind: 'start', generation: null } : { kind: 'relink', generation }))}>{generation === null ? 'Connect personal account' : 'Relink account'}</Button> : null}
      {generation !== null && status?.state !== 'disconnected' ? <Button type="button" variant="outline" className="text-base text-destructive disabled:border-muted-foreground disabled:bg-muted disabled:text-foreground disabled:opacity-100 disabled:shadow-none sm:text-sm" disabled={busy || status?.state === 'disconnecting'} onClick={scope.effect(() => void session.change({ kind: 'disconnect', generation }))}>Disconnect personal account</Button> : null}
      <Button type="button" variant="outline" className="text-base disabled:border-muted-foreground disabled:bg-muted disabled:text-foreground disabled:opacity-100 disabled:shadow-none sm:text-sm" disabled={busy} onClick={scope.effect(session.refresh)}>Refresh status</Button>
    </div>
  </section>
}

function stateText(state: PersonalStatus['state']): string {
  switch (state) {
    case 'pending': return 'Preparing a pairing code…'
    case 'qr': return 'Waiting for you to scan the pairing code.'
    case 'connected': return 'Personal account connected.'
    case 'reconnecting': return 'Reconnecting to your personal account…'
    case 'paused': return 'Personal connection paused. Relink to connect again.'
    case 'expired': return 'Pairing code expired. Relink to request a new code.'
    case 'disconnecting': return 'Disconnecting. The previous connection is still stopping.'
    case 'disconnected': return 'No personal account connected.'
    case 'failed': return 'Personal connection failed. Review the reason below before relinking.'
    default: return assertNever(state)
  }
}
function failureText(code: NonNullable<PersonalStatus['error_code']>): string {
  switch (code) {
    case 'logged_out': return 'WhatsApp signed this connection out. Relink the account.'
    case 'account_in_use': return 'This account is already linked elsewhere. Disconnect it there before retrying.'
    case 'identity_ambiguous': return 'The account identity could not be confirmed. Disconnect and relink your own account.'
    case 'auth_store_failed': return 'The connection could not be stored securely. Try again later.'
    case 'transport_failed': return 'WhatsApp could not be reached. Check connectivity and try relinking.'
    default: return assertNever(code)
  }
}
function assertNever(value: never): never { throw new TypeError(`Unknown personal state: ${typeof value}`) }
