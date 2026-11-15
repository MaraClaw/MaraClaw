import { useEffect, useMemo, useSyncExternalStore } from 'react'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Button } from '@/components/ui/button'
import { RefreshButton } from '@/components/ui/refresh-button'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { useTranslation } from '@/hooks/use-translation'
import { createPersonalSession } from '@/lib/whatsapp-personal-session'
import { changeZaloPersonalConnection, getZaloPersonalStatus, type ZaloPersonalStatus } from '@/lib/zalo-api'

export function ZaloPersonalCard({ agentId }: { readonly agentId: string }) {
  const { owned: translate, formatDate } = useTranslation()
  const scope = useWorkspaceSession()
  const session = useMemo(() => createPersonalSession({
    current: scope.isCurrent,
    read: (signal) => getZaloPersonalStatus(agentId, { owner: scope.owner, signal }),
    change: (command, signal) => changeZaloPersonalConnection(agentId, command, { owner: scope.owner, signal }),
  }), [agentId, scope])
  const view = useSyncExternalStore(session.subscribe, session.getSnapshot)
  useEffect(() => { session.start(); return session.stop }, [session])
  const status = view.status
  const generation = status?.generation ?? null
  const busy = view.busy || view.loading
  const canPair = status && ['disconnected', 'expired', 'failed', 'paused', 'connected', 'reconnecting'].includes(status.state)
  return <section aria-label={translate('Zalo personal connection')} className="min-w-0 space-y-3">
    <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">{translate('Scan with the Zalo account that will act as this agent. Other users must link their own account below before sending private messages. Self-chat is not supported.')}</p>
    <p role="status" aria-atomic="true" className="text-lg sm:text-sm">{view.loading ? translate('Loading personal connection…') : view.busy ? translate('Updating personal connection…') : status ? translate(stateText(status.state)) : ''}</p>
    {view.error ? <RequestErrorToast message={view.error} /> : null}
    {status?.account_hint ? <p className="break-all text-lg sm:text-sm">{translate('Agent account:')} {status.account_hint}</p> : null}
    {status?.error_code ? <p className="max-w-prose text-lg text-destructive sm:text-sm">{translate('Zalo could not complete the connection. Refresh status, then relink to try again.')}</p> : null}
    {status?.qr ? <div className="space-y-2">
      <img src={`data:image/png;base64,${status.qr.value}`} alt={translate('Zalo pairing QR')} width={240} height={240} className="h-auto max-w-full" />
      <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">{translate('Open Zalo on your phone, scan this code and approve the login. Do not share the code.')}</p>
      <p className="text-base text-muted-foreground sm:text-sm">{translate('Expires at')}{' '}{formatDate(status.qr.expires_at, { hour: '2-digit', minute: '2-digit' })}{translate('. An expired code is removed automatically.')}</p>
    </div> : null}
    <div className="flex flex-wrap gap-3">
      {canPair ? <Button type="button" disabled={busy} onClick={scope.effect(() => void session.change(generation === null ? { kind: 'start', generation: null } : { kind: 'relink', generation }))}>{generation === null ? translate('Connect Zalo account') : translate('Relink Zalo account')}</Button> : null}
      {generation !== null && status?.state !== 'disconnected' ? <Button type="button" variant="outline" className="text-destructive" disabled={busy || status?.state === 'disconnecting'} onClick={scope.effect(() => void session.change({ kind: 'disconnect', generation }))}>{translate('Disconnect Zalo account')}</Button> : null}
      <RefreshButton label={translate('Refresh connection status')} disabled={busy} onClick={scope.effect(session.refresh)} />
    </div>
  </section>
}

function stateText(state: ZaloPersonalStatus['state']): string {
  switch (state) {
    case 'pending': return 'Preparing a Zalo pairing code…'
    case 'qr': return 'Waiting for you to scan the Zalo pairing code.'
    case 'connected': return 'Zalo agent account connected.'
    case 'reconnecting': return 'Reconnecting to the Zalo account…'
    case 'paused': return 'Zalo connection paused. Relink to connect again.'
    case 'expired': return 'Pairing code expired. Relink to request a new code.'
    case 'disconnecting': return 'Disconnecting. The previous connection is still stopping.'
    case 'disconnected': return 'No Zalo agent account connected.'
    case 'failed': return 'Zalo connection failed. Refresh status before relinking.'
    default: return assertNever(state)
  }
}
function assertNever(value: never): never { throw new TypeError(`Unknown personal state: ${typeof value}`) }
