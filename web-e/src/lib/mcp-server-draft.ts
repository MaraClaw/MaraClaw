import type { McpServerCreate, McpServerPatch, McpServerSummary } from './agent-mcp-api.ts'

export type McpSecretField = {
  readonly id: string
  readonly key: string
  readonly value: string
  readonly stored: boolean
}
export type McpServerDraft = {
  readonly name: string
  readonly transport: McpServerSummary['transport']
  readonly enabled: boolean
  readonly url: string
  readonly command: string
  readonly args: readonly { readonly id: string; readonly value: string }[]
  readonly replaceArgs: boolean
  readonly headers: readonly McpSecretField[]
  readonly env: readonly McpSecretField[]
}

export function createMcpServerDraft(server?: McpServerSummary): McpServerDraft {
  return {
    name: server?.name ?? '', transport: server?.transport ?? 'streamable-http', enabled: server?.enabled ?? true,
    url: '', command: '', args: [], replaceArgs: false,
    headers: (server?.header_names ?? []).map((key) => ({ id: crypto.randomUUID(), key, value: '', stored: true })),
    env: (server?.env_names ?? []).map((key) => ({ id: crypto.randomUUID(), key, value: '', stored: true })),
  }
}

export function mcpServerConfiguration(draft: McpServerDraft): McpServerCreate {
  const common = { name: draft.name, transport: draft.transport, enabled: draft.enabled }
  switch (draft.transport) {
    case 'streamable-http':
    case 'sse':
      return { ...common, url: draft.url, headers: Object.fromEntries(draft.headers.filter((field) => !field.stored || field.value !== '').map((field) => [field.key, field.value])) }
    case 'stdio':
      return { ...common, command: draft.command, args: draft.args.map((arg) => arg.value), env: Object.fromEntries(draft.env.filter((field) => !field.stored || field.value !== '').map((field) => [field.key, field.value])) }
    default:
      return draft.transport satisfies never
  }
}

export function mcpServerChanges(draft: McpServerDraft, server: McpServerSummary): McpServerPatch {
  const { name: _name, transport: _transport, url, command, args, ...config } = mcpServerConfiguration(draft)
  switch (server.transport) {
    case 'streamable-http':
    case 'sse':
      return {
        ...config, ...(url ? { url } : {}),
        removed_headers: server.header_names.filter((key) => !draft.headers.some((field) => field.key.toLowerCase() === key.toLowerCase())),
      }
    case 'stdio':
      return {
        ...config, ...(command ? { command } : {}), ...(draft.replaceArgs ? { args } : {}),
        removed_env: server.env_names.filter((key) => !draft.env.some((field) => field.key === key)),
      }
    default:
      return server.transport satisfies never
  }
}
