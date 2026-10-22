import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'

import { adminSession } from '../src/lib/admin-session.ts'
import { ApiError } from '../src/lib/http.ts'
import { createTool, deleteTool, listTools, updateTool } from '../src/lib/tools-api.ts'
import type { ToolCatalog, ToolCreateInput, ToolUpdateInput } from '../src/lib/tools-api.ts'
import { MemoryStorage } from './helpers.ts'

function ownToolsSession(context: TestContext) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: new MemoryStorage() } })
  adminSession.replace('synthetic-tools-token')
  context.after(() => {
    adminSession.logout()
    if (previous) Object.defineProperty(globalThis, 'window', previous)
    else Reflect.deleteProperty(globalThis, 'window')
  })
}

const catalogTool: ToolCatalog = {
  id: 'search', name: 'search', display_name: 'Search', description: 'Search documents',
  type: 'builtin', category: 'search', icon: 'search', enabled: true, is_default: false, source: 'builtin',
  tenant_id: null, capabilities: { can_edit_details: false, can_delete: false, can_configure: true },
  parameters_schema: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] },
  mcp_server_url: null, mcp_server_name: null, mcp_tool_name: null, created_at: null,
  config: { api_key: '********', nested: { values: [null, false, 2, 'value'] } },
  config_schema: { type: 'object', properties: { api_key: { type: 'string', secret: true } } },
}

test('listTools uses the authenticated catalog endpoint and preserves nullable fields and JSON', async (context) => {
  // Given
  ownToolsSession(context)
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json([catalogTool]))
  // When
  const result = await listTools()
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/tools')
  assert.equal(options?.method, 'GET')
  assert.equal(options?.body, undefined)
  assert.deepEqual(options?.headers, { Accept: 'application/json', Authorization: 'Bearer synthetic-tools-token' })
  assert.deepEqual(result, [catalogTool])
})

test('listTools encodes the optional tenant as one query value', async (context) => {
  // Given
  ownToolsSession(context)
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json([]))
  // When
  const result = await listTools('tenant/a &?#=')
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[0], '/api/tools?tenant_id=tenant%2Fa%20%26%3F%23%3D')
  assert.deepEqual(result, [])
})

test('listTools preserves company ownership and independent capability decisions', async (context) => {
  // Given
  ownToolsSession(context)
  const tool: ToolCatalog = {
    ...catalogTool, type: 'mcp', source: 'custom', tenant_id: 'other-company',
    capabilities: { can_edit_details: true, can_delete: true, can_configure: false },
  }
  context.mock.method(globalThis, 'fetch', async () => Response.json([tool]))
  // When
  const [result] = await listTools('selected-company')
  // Then
  assert.equal(result?.tenant_id, 'other-company')
  assert.deepEqual(result?.capabilities, { can_edit_details: true, can_delete: true, can_configure: false })
})

test('createTool sends required metadata without synthesizing config or defaults', async (context) => {
  // Given
  ownToolsSession(context)
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ id: 'tool-1', name: 'custom-tool' }))
  // When
  const result = await createTool({ name: 'custom-tool', display_name: 'Custom tool' })
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/tools')
  assert.equal(options?.method, 'POST')
  assert.equal(options?.body, '{"name":"custom-tool","display_name":"Custom tool"}')
  assert.deepEqual(options?.headers, { Accept: 'application/json', Authorization: 'Bearer synthetic-tools-token', 'Content-Type': 'application/json' })
  assert.deepEqual(result, { id: 'tool-1', name: 'custom-tool' })
})

test('createTool sends snake-case optional metadata and tenant scope only in the body', async (context) => {
  // Given
  ownToolsSession(context)
  const input: ToolCreateInput = {
    name: 'remote-search', display_name: 'Remote search', description: 'Company search', type: 'mcp',
    category: 'custom', icon: 'search', parameters_schema: { type: 'object', additionalProperties: false },
    mcp_server_url: 'https://mcp.example.test/search', mcp_server_name: 'company', mcp_tool_name: null,
    is_default: true, tenant_id: 'tenant/a &?#=',
  }
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ id: 'tool-2', name: 'remote-search' }))
  // When
  await createTool(input)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/tools')
  assert.equal(options?.method, 'POST')
  assert.equal(options?.body, JSON.stringify(input))
})

for (const input of [
  { display_name: 'Renamed', enabled: false, is_default: false },
  { description: 'Scoped update', icon: null, tenant_id: 'tenant/a &?#=' },
] satisfies readonly ToolUpdateInput[]) {
  test(`updateTool preserves metadata-only writes ${'tenant_id' in input ? 'with' : 'without'} tenant scope`, async (context) => {
    // Given
    ownToolsSession(context)
    const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ ok: true }))
    // When
    const result = await updateTool('tool/a ?#&=', input)
    // Then
    const [url, options] = fetch.mock.calls[0]?.arguments ?? []
    assert.equal(url, '/api/tools/tool%2Fa%20%3F%23%26%3D')
    assert.equal(options?.method, 'PUT')
    assert.equal(options?.body, JSON.stringify(input))
    assert.deepEqual(result, { ok: true })
  })
}

test('updateTool forwards explicitly supplied config unchanged, including masked values', async (context) => {
  // Given
  ownToolsSession(context)
  const input: ToolUpdateInput = {
    config: { api_key: '********', enabled: false, nested: { values: [null, 1, 'value'] } },
    parameters_schema: { type: 'object' }, mcp_server_url: null, mcp_server_name: 'company',
    display_name: null, description: null, is_default: null, tenant_id: null,
  }
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ ok: true }))
  // When
  const result = await updateTool('tool-1', input)
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/tools/tool-1')
  assert.equal(options?.method, 'PUT')
  assert.equal(options?.body, JSON.stringify(input))
  assert.deepEqual(result, { ok: true })
})

test('deleteTool encodes the id and sends DELETE without a body or tenant query', async (context) => {
  // Given
  ownToolsSession(context)
  const fetch = context.mock.method(globalThis, 'fetch', async (_url: RequestInfo | URL, _options?: RequestInit) => Response.json({ ok: true }))
  // When
  const result = await deleteTool('tool/a ?#&=')
  // Then
  const [url, options] = fetch.mock.calls[0]?.arguments ?? []
  assert.equal(url, '/api/tools/tool%2Fa%20%3F%23%26%3D')
  assert.equal(options?.method, 'DELETE')
  assert.equal(options?.body, undefined)
  assert.deepEqual(result, { ok: true })
})

test('listTools propagates caller abort through the session-owned transport signal', async (context) => {
  // Given
  ownToolsSession(context)
  const controller = new AbortController()
  const response = Promise.withResolvers<Response>()
  const fetch = context.mock.method(globalThis, 'fetch', (_url: RequestInfo | URL, _options?: RequestInit) => response.promise)
  const pending = listTools('tenant-1', controller.signal)
  const rejected = assert.rejects(pending, (error) => error instanceof DOMException && error.name === 'AbortError')
  // When
  controller.abort()
  response.resolve(Response.json([]))
  await rejected
  // Then
  assert.equal(fetch.mock.calls[0]?.arguments[1]?.signal?.aborted, true)
})

test('updateTool preserves API errors from the shared HTTP client', async (context) => {
  // Given
  ownToolsSession(context)
  context.mock.method(globalThis, 'fetch', async () => Response.json({ detail: 'Tool not found' }, { status: 404 }))
  // When / Then
  await assert.rejects(updateTool('missing', { enabled: false }), (error) => error instanceof ApiError && error.status === 404 && error.detail === 'Tool not found')
})
