import type { ToolCatalog } from '../lib/tools-api.ts'

export function toolOwnership(tool: Pick<ToolCatalog, 'source' | 'tenant_id'>): 'Built-in' | 'Global' | 'Company' {
  if (tool.source === 'builtin') return 'Built-in'
  return tool.tenant_id === null ? 'Global' : 'Company'
}

export function toolRecordImpact(tool: Pick<ToolCatalog, 'source' | 'tenant_id'>): string {
  return toolOwnership(tool) === 'Company'
    ? 'Changes to this company-owned record affect its owning company and agents assigned to the tool, not other companies.'
    : 'Changes to this shared catalog record apply across companies and to agents assigned to the tool.'
}

export function toolActions(tool: Pick<ToolCatalog, 'type' | 'capabilities'>, concreteCompany: boolean) {
  return {
    editDetails: tool.capabilities.can_edit_details,
    configure: concreteCompany && tool.capabilities.can_configure,
    delete: tool.type !== 'builtin' && tool.capabilities.can_delete,
  }
}
