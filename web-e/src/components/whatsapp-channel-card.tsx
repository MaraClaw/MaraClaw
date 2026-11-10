import { useState, type ReactNode } from 'react'

import { RequestErrorToast } from '@/components/request-error-toast'
import { Button } from '@/components/ui/button'
import { WhatsAppPersonalCard } from '@/components/whatsapp-personal-card'
import { useQuery, useWorkspaceSession } from '@/hooks/session-workspace'
import { getWhatsAppMode, type WhatsAppMode } from '@/lib/whatsapp-api'

export function WhatsAppChannelCard({ agentId, cloud }: { readonly agentId: string; readonly cloud: (saved: () => void) => ReactNode }) {
  const scope = useWorkspaceSession()
  const [selected, setSelected] = useState<WhatsAppMode | null>(null)
  const mode = useQuery({
    queryKey: ['whatsapp-mode', agentId],
    queryFn: ({ signal }) => getWhatsAppMode(agentId, { owner: scope.owner, signal }),
    retry: false,
  })
  if (mode.isPending) return <p role="status">Loading WhatsApp setup…</p>
  if (mode.isError) return <div className="space-y-2"><RequestErrorToast error={mode.error} message="Unable to load WhatsApp setup." /><Button type="button" variant="outline" className="text-base sm:text-sm" onClick={scope.effect(() => void mode.refetch())}>Retry WhatsApp setup</Button></div>
  const value = selected ?? mode.data
  return <div className="min-w-0 space-y-3">
    <fieldset className="flex flex-wrap gap-3">
      <legend className="mb-2 text-base font-medium sm:text-sm">Connection method</legend>
      {(['cloud', 'personal_qr'] as const).map((choice) => <label key={choice} className="flex min-h-11 cursor-pointer items-center gap-2 rounded-xl border border-border px-3 text-base has-[:checked]:bg-accent has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring sm:text-sm">
        <input type="radio" name={`${agentId}-whatsapp-mode`} value={choice} checked={value === choice} onChange={scope.effect(() => setSelected(choice))} />
        {choice === 'cloud' ? 'Cloud API' : 'Personal QR'}
      </label>)}
    </fieldset>
    {value === 'personal_qr' ? <WhatsAppPersonalCard key={agentId} agentId={agentId} /> : <>
      {mode.data === 'personal_qr' ? <p className="max-w-prose text-lg text-muted-foreground sm:text-sm">Saving Cloud API credentials replaces the personal connection. Only one WhatsApp mode and account can be active.</p> : null}
      {cloud(scope.effect(() => void mode.refetch()))}
    </>}
  </div>
}
