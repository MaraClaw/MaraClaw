import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { createAgentMcpServer, listAgentMcpServers, patchAgentMcpServer, removeAgentMcpServer } from '../src/lib/agent-mcp-api.ts'
import { authSession } from '../src/lib/auth-session.ts'
import { MCPServerSummarySchema } from '../src/lib/engine-contracts.generated.ts'
import { ApiError, StaleSessionError } from '../src/lib/http.ts'

const summary = MCPServerSummarySchema.parse({
  name: 'research', transport: 'streamable-http', enabled: true, url_configured: true, command_configured: false,
  args_count: 0, header_names: ['Authorization'], env_names: [], connection_timeout_ms: null, request_timeout_ms: 30000,
})

test('MCP list strips credential values before returning safe summaries and forwards cancellation', async (t) => {
  // Given
  const controller = new AbortController()
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    assert.equal(String(input), '/api/agents/agent%2Fone/mcp/servers')
    assert.equal(init?.method, 'GET')
    assert.equal(init?.signal, controller.signal)
    assert.equal(init?.cache, 'no-store')
    return Response.json({ servers: [{ ...summary, url: 'https://private.example/?token=private', command: 'private', args: ['private'], headers: { Authorization: 'private' }, env: { TOKEN: 'private' } }] })
  })
  // When / Then
  assert.deepEqual(await listAgentMcpServers('agent/one', { signal: controller.signal }), { servers: [summary] })
})

test('MCP create, merged patch and delete use the dedicated encoded protocol', async (t) => {
  // Given
  const requests: { readonly path: string; readonly method: string; readonly body: unknown }[] = []
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({ path: String(input), method: init?.method ?? '', body: init?.body ? JSON.parse(String(init.body)) : null })
    return init?.method === 'DELETE' ? new Response(null, { status: 204 }) : Response.json(summary)
  })
  const create = { name: 'team-server', transport: 'stdio', enabled: false, command: 'node', args: ['server.js', 'two words'], env: { TOKEN: 'new-value' } } as const
  const patch = { enabled: true, headers: { Authorization: 'replacement' }, removed_headers: ['X-Old'] }
  const target = { agentId: 'agent/one', name: 'team/server' }
  // When
  assert.deepEqual(await createAgentMcpServer(target.agentId, create), { sync: 'applied', value: summary })
  assert.deepEqual(await patchAgentMcpServer(target, patch), { sync: 'applied', value: summary })
  assert.deepEqual(await removeAgentMcpServer(target), { sync: 'applied', value: undefined })
  // Then
  assert.deepEqual(requests, [
    { path: '/api/agents/agent%2Fone/mcp/servers', method: 'POST', body: create },
    { path: '/api/agents/agent%2Fone/mcp/servers/team%2Fserver', method: 'PATCH', body: patch },
    { path: '/api/agents/agent%2Fone/mcp/servers/team%2Fserver', method: 'DELETE', body: null },
  ])
})

const mutations = [
  { method: 'POST', run: () => createAgentMcpServer('agent', { name: 'research', transport: 'sse', url: 'https://mcp.example.test' }) },
  { method: 'PATCH', run: () => patchAgentMcpServer({ agentId: 'agent', name: 'research' }, { enabled: false }) },
  { method: 'DELETE', run: () => removeAgentMcpServer({ agentId: 'agent', name: 'research' }) },
] as const

for (const mutation of mutations) {
  test(`MCP ${mutation.method} reports a committed 503 as pending without retrying or exposing sync details`, async (t) => {
    // Given
    const requests: string[] = []
    t.mock.method(globalThis, 'fetch', async (_input: string | URL | Request, init?: RequestInit) => {
      requests.push(init?.method ?? '')
      return Response.json({ detail: { code: 'mcp_saved_sync_pending', saved: true, sync_error: 'mcp_config_write_failed', credentials: 'private' } }, { status: 503 })
    })
    // When / Then
    assert.deepEqual(await mutation.run(), { sync: 'pending' })
    assert.deepEqual(requests, [mutation.method])
  })
}

test('MCP mutations preserve unknown or uncommitted 5xx errors', async (t) => {
  // Given
  let detail: unknown
  t.mock.method(globalThis, 'fetch', async () => Response.json({ detail }, { status: 503 }))
  // When / Then
  for (const rejected of [
    'Guest unavailable', { saved: true }, { code: 'other_failure', saved: true },
    { code: 'mcp_saved_sync_pending', saved: false }, { code: 'mcp_saved_sync_pending', saved: 'true' },
  ]) {
    detail = rejected
    await assert.rejects(mutations[0].run(), (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.status, 503)
      assert.deepEqual(error.detail, detail)
      return true
    })
  }
})

test('malformed MCP list and denied reads remain errors rather than empty lists', async (t) => {
  // Given
  let response = Response.json({ servers: [{ ...summary, args_count: 'hidden' }] })
  t.mock.method(globalThis, 'fetch', async () => response)
  // When / Then
  await assert.rejects(listAgentMcpServers('agent'), ZodError)
  response = Response.json({ detail: 'Manage access required' }, { status: 403 })
  await assert.rejects(listAgentMcpServers('agent'), (error: unknown) => error instanceof ApiError && error.status === 403)
})

test('a late MCP mutation response is rejected after session replacement', async (t) => {
  // Given
  const pending = Promise.withResolvers<Response>()
  t.mock.method(globalThis, 'fetch', () => pending.promise)
  const request = patchAgentMcpServer({ agentId: 'agent', name: 'research' }, { enabled: false })
  t.after(() => { authSession.adoptStoredSession(authSession.capture()) })
  // When
  authSession.adoptStoredSession(authSession.capture())
  pending.resolve(Response.json(summary))
  // Then
  await assert.rejects(request, StaleSessionError)
})
