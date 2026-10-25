import { useQueryClient } from '@tanstack/react-query'
import { useLayoutEffect, useRef, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { revokeInvitationOptions } from '@/hooks/invitation-options'
import { useSessionMutation } from '@/hooks/use-session-mutation'
import { type InvitationCode } from '@/lib/invitations-api'

const statuses = {
  active: { label: 'Active', variant: 'success' }, expired: { label: 'Expired', variant: 'warning' },
  used: { label: 'Used', variant: 'secondary' }, revoked: { label: 'Revoked', variant: 'destructive' },
} as const
const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
const cell = 'min-w-0 px-4 py-2 align-top lg:py-4'
const label = 'mb-1 block text-xs font-medium text-muted-foreground lg:hidden'

function InvitationTime({ value }: { readonly value: string | null }) {
  return value ? <time dateTime={value}>{dateFormat.format(new Date(value))}</time> : <>—</>
}

export function InvitationsRow({ item, tenant }: { readonly item: InvitationCode; readonly tenant: string | null }) {
  const [confirming, setConfirming] = useState(false)
  const confirm = useRef<HTMLParagraphElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const statusFeedback = useRef<HTMLTableCellElement>(null)
  const restore = useRef(false)
  const revoking = useSessionMutation(revokeInvitationOptions(useQueryClient(), tenant))
  useLayoutEffect(() => {
    if (confirming) confirm.current?.focus()
    else if (restore.current) { trigger.current?.focus(); restore.current = false }
  }, [confirming])
  useLayoutEffect(() => {
    if (revoking.isSuccess && item.status === 'revoked') statusFeedback.current?.focus()
  }, [item.status, revoking.isSuccess])
  const status = statuses[item.status]
  return (
    <tr className="grid min-w-0 gap-y-1 border-t border-border py-3 first:border-t-0 lg:table-row lg:py-0">
      <td className={cell}><span className={label}>Recipient / code</span><p className="font-medium">{item.invited_email ?? 'Not email-bound (legacy)'}</p><code className="mt-1 block text-xs text-muted-foreground [overflow-wrap:anywhere]">{item.code}</code></td>
      <td className={cell}><span className={label}>Created</span><InvitationTime value={item.created_at} /></td>
      <td className={cell}><span className={label}>Expires</span>{item.expires_at ? <InvitationTime value={item.expires_at} /> : 'No expiry (legacy)'}</td>
      <td className={cell}><span className={label}>Use</span><p>{item.used_count} / {item.max_uses}</p>{item.redeemed_at ? <p className="mt-1 text-xs text-muted-foreground">Redeemed <InvitationTime value={item.redeemed_at} /></p> : null}</td>
      <td ref={statusFeedback} tabIndex={-1} className={`${cell} focus-visible:outline-2 focus-visible:outline-ring`}><span className={label}>Status</span><Badge variant={status.variant}>{status.label}</Badge></td>
      <td className={cell}>
        {item.status === 'active' ? confirming ? (
          <section aria-label="Revoke invitation confirmation" className="flex flex-col gap-2" onKeyDown={(event) => {
            if (event.key === 'Escape' && !revoking.isPending) { event.preventDefault(); restore.current = true; setConfirming(false) }
          }}>
            <p ref={confirm} tabIndex={-1} className="font-medium focus-visible:outline-2 focus-visible:outline-ring">Revoke invitation?</p>
            <p className="text-xs text-muted-foreground">It will no longer be usable.</p>
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" className="min-h-11" disabled={revoking.isPending} onClick={() => { restore.current = true; setConfirming(false) }}>Cancel</Button>
              <Button type="button" variant="destructive" className="min-h-11" disabled={revoking.isPending} onClick={() => revoking.mutate(item.id)}>{revoking.isPending ? 'Revoking…' : 'Confirm revoke'}</Button>
            </div>
            {revoking.isError ? <p role="alert" className="text-sm text-destructive">Could not revoke the invitation. Refresh history before trying again.</p> : null}
          </section>
        ) : <Button ref={trigger} type="button" variant="outline" className="min-h-11" aria-label={`Revoke invitation for ${item.invited_email ?? 'legacy code'}`} onClick={() => { revoking.reset(); setConfirming(true) }}>Revoke</Button> : <span className="text-muted-foreground">—</span>}
      </td>
    </tr>
  )
}
