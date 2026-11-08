import { MCPServerSummarySchema } from '../src/lib/engine-contracts.generated'

export const mcpServer = MCPServerSummarySchema.parse({
  name: 'research', transport: 'streamable-http', enabled: true,
  url_configured: true, command_configured: false, args_count: 0,
  header_names: ['Authorization'], env_names: [], connection_timeout_ms: null, request_timeout_ms: null,
})
