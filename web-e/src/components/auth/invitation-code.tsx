import { useId } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { useInvitationPreview } from '@/hooks/use-invitation-preview'

export function InvitationCode({ invitation, disabled }: {
  readonly invitation: ReturnType<typeof useInvitationPreview>
  readonly disabled: boolean
}) {
  const id = useId()
  return <form className="mb-4 space-y-4" onSubmit={(event) => { event.preventDefault(); void invitation.validate() }}>
    <div className="space-y-2">
      <Label htmlFor={id}>Invitation code</Label>
      <Input id={id} required maxLength={256} autoComplete="off" value={invitation.code} disabled={disabled}
        aria-invalid={Boolean(invitation.error)} aria-describedby={invitation.error ? `${id}-error` : undefined}
        onChange={(event) => invitation.edit(event.currentTarget.value)} />
      {invitation.error ? <p id={`${id}-error`} role="alert" className="text-sm text-destructive">{invitation.error}</p> : null}
    </div>
    <Button type="submit" variant={invitation.preview ? 'outline' : 'default'} className="h-11 w-full" disabled={disabled || invitation.busy || !invitation.code.trim()}>
      {invitation.busy ? 'Validating invitation…' : 'Validate invitation'}
    </Button>
    {invitation.preview ? <p role="status" className="rounded-xl border border-border bg-muted/50 p-4 text-sm [overflow-wrap:anywhere]">Destination: <strong>{invitation.preview.tenant.name}</strong></p> : null}
  </form>
}
