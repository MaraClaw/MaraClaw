import assert from 'node:assert/strict'
import { test } from 'node:test'

import type { McpServerSummary } from '../src/lib/agent-mcp-api.ts'
import { createMcpServerDraft, mcpServerChanges, mcpServerConfiguration } from '../src/lib/mcp-server-draft.ts'
import { MCPServerCreateSchema, MCPServerPatchSchema, MCPServerSummarySchema } from '../src/lib/engine-contracts.generated.ts'

const server: McpServerSummary = MCPServerSummarySchema.parse({
  name: 'research', transport: 'streamable-http', enabled: true, url_configured: true, command_configured: false,
  args_count: 0, header_names: ['Authorization', 'X-Remove'], env_names: [], connection_timeout_ms: null, request_timeout_ms: null,
})

test('editing a sanitized server preserves hidden URL and saved secrets while removing only explicitly deleted keys', () => {
  // Given
  const initial = createMcpServerDraft(server)
  assert.ok(initial.headers.every((field) => field.value === ''))
  assert.equal(initial.url, '')
  const draft = { ...initial, enabled: false, headers: initial.headers.filter((field) => field.key !== 'X-Remove') }
  // When / Then
  assert.deepEqual(mcpServerChanges(draft, server), {
    enabled: false, headers: {}, removed_headers: ['X-Remove'],
  })
})

test('replacement credentials and SSE URL changes are explicit while unchanged credential fields stay omitted', () => {
  // Given
  const sse = { ...server, transport: 'sse' } as const
  const initial = createMcpServerDraft(sse)
  const draft = {
    ...initial, url: 'https://mcp.example.test/events',
    headers: initial.headers.map((field) => field.key === 'Authorization' ? { ...field, value: 'replacement' } : field),
  } as const
  // When / Then
  assert.deepEqual(MCPServerPatchSchema.parse(mcpServerChanges(draft, sse)), {
    enabled: true, url: 'https://mcp.example.test/events', headers: { Authorization: 'replacement' }, removed_headers: [],
  })
})

test('stdio configuration preserves argument boundaries and sends only compatible environment fields', () => {
  // Given
  const draft = {
    ...createMcpServerDraft(), name: 'local', transport: 'stdio', command: 'node', enabled: false,
    args: [{ id: 'one', value: 'server.js' }, { id: 'two', value: 'two words' }, { id: 'three', value: '' }],
    headers: [{ id: 'header', key: 'Authorization', value: 'incompatible', stored: false }],
    env: [{ id: 'env', key: 'TOKEN', value: 'value', stored: false }],
  } as const
  // When / Then
  assert.deepEqual(MCPServerCreateSchema.parse(mcpServerConfiguration(draft)), { name: 'local', transport: 'stdio', enabled: false, command: 'node', args: ['server.js', 'two words', ''], env: { TOKEN: 'value' } })
})

test('redacted stdio editing preserves command and argv unless replacement is explicitly selected', () => {
  // Given
  const stdio = { ...server, transport: 'stdio', url_configured: false, command_configured: true, args_count: 2, header_names: [], env_names: ['TOKEN', 'REMOVE'] } as const
  const initial = createMcpServerDraft(stdio)
  assert.equal(initial.command, '')
  assert.deepEqual(initial.args, [])
  // When / Then
  assert.deepEqual(mcpServerChanges(initial, stdio), { enabled: true, env: {}, removed_env: [] })
  assert.deepEqual(mcpServerChanges({ ...initial, replaceArgs: true, env: initial.env.filter((field) => field.key !== 'REMOVE') }, stdio), {
    enabled: true, args: [], env: {}, removed_env: ['REMOVE'],
  })
  assert.deepEqual(mcpServerChanges({ ...initial, command: 'python', replaceArgs: true, args: [{ id: 'one', value: 'two words' }] }, stdio), {
    enabled: true, command: 'python', args: ['two words'], env: {}, removed_env: [],
  })
})
