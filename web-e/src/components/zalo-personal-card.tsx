import { useEffect, useMemo, useSyncExternalStore } from 'react'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Button } from '@/components/ui/button'
import { RefreshButton } from '@/components/ui/refresh-button'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { useTranslation } from '@/hooks/use-translation'
import { createPersonalSession } from '@/lib/whatsapp-personal-session'
import { changeZaloPersonalConnection, getZaloPersonalStatus, type ZaloPersonalStatus } from '@/lib/zalo-api'

export function ZaloPersonalCard({ agentId }: { readonly agentId: string }) {
  const { owned: translate, formatDate, formatNumber } = useTranslation()
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
  const diagnostics = status?.diagnostics
  const canResume = status?.resumable && generation !== null && (status.state === 'paused' || status.state === 'failed')
  const canPair = status && ['disconnected', 'expired', 'failed', 'paused', 'connected', 'reconnecting'].includes(status.state)
  return <section aria-label={translate('Zalo personal connection')} className="min-w-0 space-y-3">
    <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">{translate('Scan with the Zalo account that will act as this agent. Other users must link their own account below before sending private messages. Self-chat is not supported.')}</p>
    <p role="status" aria-atomic="true" className="text-lg sm:text-sm">{view.loading ? translate('Loading personal connection…') : view.busy ? translate('Updating personal connection…') : status ? translate(stateText(status.state)) : ''}</p>
    {diagnostics ? <dl aria-label={translate('Connection diagnostics')} className="space-y-2 text-base sm:text-sm">
      <div className="flex flex-wrap gap-x-3 gap-y-1"><dt className="text-muted-foreground">{translate('Recovery attempts')}</dt><dd className="tabular-nums">{formatNumber(diagnostics.reconnect_attempts)}</dd></div>
      {([
        ['Next retry', diagnostics.retry_at, 'Not scheduled'],
        ['Connected since', diagnostics.connected_at, 'Not recorded'],
        ['Last activity', diagnostics.last_activity_at, 'Not recorded'],
        ['Last heartbeat', diagnostics.heartbeat_at, 'Not recorded'],
      ] as const).map(([label, value, empty]) => <div key={label} className="flex flex-wrap gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">{translate(label)}</dt>
        <dd className="min-w-0 break-words tabular-nums">{value ? <time dateTime={value}>{formatDate(value, { dateStyle: 'medium', timeStyle: 'medium' })}</time> : translate(empty)}</dd>
      </div>)}
      <div className="flex flex-wrap gap-x-3 gap-y-1"><dt className="text-muted-foreground">{translate('Heartbeat failures')}</dt><dd className="tabular-nums">{formatNumber(diagnostics.heartbeat_failures)}</dd></div>
      {diagnostics.last_error ? <div className="space-y-1">
        <dt className="text-muted-foreground">{translate('Last problem')}</dt>
        <dd className="max-w-prose">{translate(problemText(diagnostics.last_error))}{diagnostics.last_error_at ? <>{' '}<time dateTime={diagnostics.last_error_at} className="tabular-nums">{formatDate(diagnostics.last_error_at, { dateStyle: 'medium', timeStyle: 'medium' })}</time></> : null}</dd>
      </div> : null}
    </dl> : null}
    {view.error ? <RequestErrorToast message={view.error} /> : null}
    {status?.account_hint ? <p className="break-all text-lg sm:text-sm">{translate('Agent account:')} {status.account_hint}</p> : null}
    {status?.error_code && !diagnostics?.last_error ? <p className="max-w-prose text-lg text-destructive sm:text-sm">{translate('Zalo could not complete the connection. Refresh status and review the available connection actions.')}</p> : null}
    {status?.qr ? <div className="space-y-2">
      <img src={`data:image/png;base64,${status.qr.value}`} alt={translate('Zalo pairing QR')} width={240} height={240} className="h-auto max-w-full" />
      <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">{translate('Open Zalo on your phone, scan this code and approve the login. Do not share the code.')}</p>
      <p className="text-base text-muted-foreground sm:text-sm">{translate('Expires at')}{' '}{formatDate(status.qr.expires_at, { hour: '2-digit', minute: '2-digit' })}{translate('. An expired code is removed automatically.')}</p>
    </div> : null}
    <div className="flex flex-wrap gap-3">
      {canPair && generation === null ? <Button type="button" disabled={busy} onClick={scope.effect(() => void session.change({ kind: 'start', generation: null }))}>{translate('Connect Zalo account')}</Button> : null}
      {canResume ? <Button type="button" disabled={busy} onClick={scope.effect(() => void session.change({ kind: 'start', generation }))}>{translate('Reconnect Zalo account')}</Button> : null}
      {canPair && generation !== null ? <Button type="button" variant="outline" className="text-destructive" disabled={busy} onClick={scope.effect(() => void session.change({ kind: 'relink', generation }))}>{translate('Relink Zalo account')}</Button> : null}
      {generation !== null && status?.state !== 'disconnected' ? <Button type="button" variant="outline" className="text-destructive" disabled={busy || status?.state === 'disconnecting'} onClick={scope.effect(() => void session.change({ kind: 'disconnect', generation }))}>{translate('Disconnect Zalo account')}</Button> : null}
      <RefreshButton label={translate('Refresh connection status')} disabled={busy} onClick={scope.effect(session.refresh)} />
    </div>
    {canPair && generation !== null ? <p className="max-w-prose text-base text-muted-foreground sm:text-sm">{translate('Relinking clears the saved login and requests a new pairing code.')}</p> : null}
  </section>
}

function stateText(state: ZaloPersonalStatus['state']): string {
  switch (state) {
    case 'pending': return 'Preparing a Zalo pairing code…'
    case 'qr': return 'Waiting for you to scan the Zalo pairing code.'
    case 'connected': return 'Zalo agent account connected.'
    case 'reconnecting': return 'Reconnecting to the Zalo account…'
    case 'paused': return 'Zalo connection paused.'
    case 'expired': return 'Pairing code expired. Relink to request a new code.'
    case 'disconnecting': return 'Disconnecting. The previous connection is still stopping.'
    case 'disconnected': return 'No Zalo agent account connected.'
    case 'failed': return 'Zalo connection failed.'
    default: return assertNever(state)
  }
}
function problemText(code: NonNullable<NonNullable<ZaloPersonalStatus['diagnostics']>['last_error']>): string {
  switch (code) {
    case 'account_in_use': return 'This Zalo account is already connected elsewhere.'
    case 'identity_ambiguous': return 'The Zalo account identity could not be confirmed.'
    case 'protocol_error': return 'Zalo returned an unexpected connection response.'
    case 'login_failed': return 'The Zalo login could not be completed.'
    case 'auth_store_failed': return 'The Zalo login could not be stored securely.'
    case 'transport_failed': return 'The connection to Zalo was interrupted.'
    case 'qr_expired': return 'The Zalo pairing code expired.'
    case 'qr_declined': return 'The Zalo pairing request was declined.'
    case 'lease_expired': return 'The Zalo connection stopped responding.'
    case 'session_replaced': return 'Zalo web session ended or was replaced'
    default: return assertNever(code)
  }
}
function assertNever(value: never): never { throw new TypeError(`Unknown personal state: ${typeof value}`) }
