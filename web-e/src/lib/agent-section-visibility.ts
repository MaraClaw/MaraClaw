import { matchPath } from 'react-router-dom'

type AgentSectionTab = { readonly id: string; readonly to: string }

export function visibleAgentTabs<Tab extends AgentSectionTab>(
  tabs: readonly Tab[],
  hiddenSections: readonly string[],
  accessLevel: string | undefined,
): Tab[] {
  return tabs.filter((tab) =>
    !hiddenSections.includes(tab.id) && (tab.id !== 'activity' || accessLevel === 'manage'))
}

/** Path of the first visible tab when `pathname` is inside a section the company hides, otherwise null. */
export function hiddenSectionRedirect<Tab extends AgentSectionTab>(
  tabs: readonly Tab[],
  hiddenSections: readonly string[],
  visibleTabs: readonly Tab[],
  pathname: string,
): string | null {
  const first = visibleTabs[0]
  if (!first) return null
  for (const tab of tabs) {
    if (!hiddenSections.includes(tab.id)) continue
    const agentId = matchPath(`/app/agents/:agentId/${tab.to}/*`, pathname)?.params.agentId
    if (agentId) return `/app/agents/${encodeURIComponent(agentId)}/${first.to}`
  }
  return null
}
