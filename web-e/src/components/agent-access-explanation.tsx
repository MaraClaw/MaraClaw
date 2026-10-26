import type { AgentAccessReason } from '@/lib/engine-contracts.generated'

const labels = {
  creator: 'You created this agent',
  administrator: 'Available through administrator access',
  company: 'Shared with your company',
  direct: 'Shared directly with you',
  department: 'Shared with your department',
} satisfies Record<AgentAccessReason, string>

export function AgentAccessExplanation({ reason }: { readonly reason: AgentAccessReason | null | undefined }) {
  return reason ? <p className="text-base text-muted-foreground md:text-sm">{labels[reason]}</p> : null
}
