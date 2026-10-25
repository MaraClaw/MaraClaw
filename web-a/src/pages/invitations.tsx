import { useAuth } from '@/hooks/use-auth'
import { InvitationsForm } from './invitations-form'
import { InvitationsHistory } from './invitations-history'

export function InvitationsPage() {
  const { user } = useAuth()
  const tenant = user?.tenant_id ?? null
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 [overflow-wrap:anywhere]">
      <header className="flex flex-col gap-2">
        <h1 className="font-display text-2xl font-semibold tracking-tight">Invitations</h1>
        <p className="text-muted-foreground">Invite someone to your current organization by email.</p>
      </header>
      <InvitationsForm key={`form-${tenant}`} tenant={tenant} />
      <InvitationsHistory key={`history-${tenant}`} tenant={tenant} />
    </div>
  )
}
