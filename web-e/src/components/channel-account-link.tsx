import { useEffect, useMemo, useSyncExternalStore } from 'react'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Button } from '@/components/ui/button'
import { RefreshButton } from '@/components/ui/refresh-button'
import { useWorkspaceSession } from '@/hooks/session-workspace'
import { useTranslation } from '@/hooks/use-translation'
import { getChannelLink, issueChannelLinkCode, unlinkChannelAccount, type LinkChannel } from '@/lib/channel-links-api'
import { createChannelLinkSession } from '@/lib/channel-links-session'

export function ChannelAccountLink({ agentId, channel }: { readonly agentId: string; readonly channel: LinkChannel }) {
  const { owned: translate, formatDate } = useTranslation()
  const scope = useWorkspaceSession()
  const session = useMemo(() => createChannelLinkSession({
    current: scope.isCurrent,
    read: (signal) => getChannelLink({ agentId, channel }, { owner: scope.owner, signal }),
    issue: (signal) => issueChannelLinkCode({ agentId, channel }, { owner: scope.owner, signal }),
    unlink: (signal) => unlinkChannelAccount({ agentId, channel }, { owner: scope.owner, signal }),
  }), [agentId, channel, scope])
  const view = useSyncExternalStore(session.subscribe, session.getSnapshot)
  useEffect(() => { session.start(); return session.stop }, [session])
  const busy = view.loading || view.busy
  const code = view.code
  return <section aria-label={translate('Your account link')} className="min-w-0 space-y-3 border-t border-border pt-4">
    <h3 className="text-base font-semibold">{translate('Link your account')}</h3>
    <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">{translate('Link your own account to use this agent in private messages. Unlinked messages do not run the agent.')}</p>
    {channel === 'zalo' ? <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">{translate('Message the Zalo agent account from a different account. The agent account cannot link through self-chat.')}</p> : null}
    <p role="status" aria-atomic="true" className="text-lg sm:text-sm">{view.loading ? translate('Loading your account link…') : view.busy ? translate('Updating your account link…') : view.status?.linked ? translate('Your account is linked.') : view.status?.available ? translate('Your account is not linked.') : view.status ? translate('The creator must configure this channel before you can link your account.') : ''}</p>
    {view.status?.linked && view.status.external_hint ? <p className="break-all text-lg sm:text-sm">{translate('Linked account:')} {view.status.external_hint}</p> : null}
    {view.status?.linked_at ? <p className="text-base text-muted-foreground sm:text-sm">{translate('Linked at')}{' '}{formatDate(view.status.linked_at, { dateStyle: 'medium', timeStyle: 'short' })}</p> : null}
    {view.error ? <RequestErrorToast message={view.error} /> : null}
    {view.expired ? <p className="text-lg sm:text-sm">{translate('Link code expired. Issue a new code to try again.')}</p> : null}
    {code ? <div className="min-w-0 space-y-2">
      {code.account_hint ? <p className="break-all text-lg sm:text-sm">{translate('Send to:')} {code.account_hint}</p> : null}
      <p className="max-w-prose text-lg sm:text-sm">{translate('Send this command in a private message:')}</p>
      <code className="block select-text break-all rounded-xl bg-muted px-3 py-2 text-base">/link {code.code}</code>
      {channel === 'telegram' ? <p className="text-base text-muted-foreground sm:text-sm">{translate('Telegram also accepts /start followed by this code.')}</p> : null}
      <p className="text-base text-muted-foreground sm:text-sm">{translate('Expires at')}{' '}{formatDate(code.expires_at, { hour: '2-digit', minute: '2-digit' })}{translate('. Keep this code private.')}</p>
      {code.deep_link ? <Button asChild variant="outline"><a href={code.deep_link} target="_blank" rel="noreferrer">{translate('Open private chat')}</a></Button> : null}
    </div> : null}
    <div className="flex flex-wrap gap-3">
      {view.status?.available && !view.status.linked ? <Button type="button" disabled={busy} onClick={scope.effect(() => void session.issue())}>{code ? translate('Replace link code') : translate('Issue link code')}</Button> : null}
      {view.status?.linked ? <Button type="button" variant="outline" disabled={busy} onClick={scope.effect(() => void session.unlink())}>{translate('Unlink my account')}</Button> : null}
      <RefreshButton label={translate('Refresh account link')} disabled={busy} onClick={scope.effect(session.refresh)} />
    </div>
  </section>
}
