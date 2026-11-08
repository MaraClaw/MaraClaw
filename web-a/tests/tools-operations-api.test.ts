import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import { z } from 'zod'
import { adminSession } from '../src/lib/admin-session.ts'
import { discoverMcpCatalog, importMcpCatalog, patchToolConfig, probeSavedTool } from '../src/lib/tools-api.ts'
import { MemoryStorage } from './helpers.ts'

function ownSession(context: TestContext) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { localStorage: new MemoryStorage() } })
  adminSession.replace('synthetic-tools-token')
  context.after(() => { adminSession.logout(); if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
}
const connection = { server_url: 'https://mcp.example.test/mcp', api_key: 'synthetic', tenant_id: 'company-a' }
const tool = { name: 'lookup', description: 'Lookup', inputSchema: { type: 'object' } }

test('configuration PATCH preserves omitted keys and encodes selected company without changing PUT', async (context) => {
  ownSession(context)
  const request = context.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  await patchToolConfig('tool/a', { tenant_id: 'company-a', set: { region: 'eu' }, unset: ['port'] })
  const [path, init] = request.mock.calls[0]?.arguments ?? []
  assert.equal(path, '/api/tools/tool%2Fa')
  assert.equal(init?.method, 'PATCH')
  assert.deepEqual(JSON.parse(String(init?.body)), { set: { region: 'eu' }, unset: ['port'], tenant_id: 'company-a' })
})

test('configuration PATCH rejects overlapping fields before sending', async (context) => {
  ownSession(context)
  const request = context.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  await assert.rejects(patchToolConfig('tool', { tenant_id: 'company-a', set: { api_key: 'new' }, unset: ['api_key'] }), z.ZodError)
  assert.equal(request.mock.callCount(), 0)
})

test('saved probe sends reference plus draft operations without a copied saved configuration', async (context) => {
  ownSession(context)
  const request = context.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true, tools: [] }))
  await probeSavedTool({ tool_id: 'tool', tenant_id: 'company-a', kind: 'mcp' }, { set: { region: 'eu' }, unset: [] })
  const [path, init] = request.mock.calls[0]?.arguments ?? []
  assert.equal(path, '/api/tools/test-mcp')
  assert.deepEqual(JSON.parse(String(init?.body)), { saved: { tool_id: 'tool', tenant_id: 'company-a' }, operations: { set: { region: 'eu' }, unset: [] } })
})

test('discovery validates the complete inventory and atomic import sends exactly the chosen definitions', async (context) => {
  ownSession(context)
  const request = context.mock.method(globalThis, 'fetch', async (path: string) => Response.json(path.endsWith('/discover') ? { ok: true, tools: [tool] } : { ok: true, tools: [{ id: 'created', name: 'team_lookup' }] }))
  const discovered = await discoverMcpCatalog(connection)
  const imported = await importMcpCatalog({ ...connection, server_name: 'Team', name_prefix: 'team', tools: discovered.tools })
  const [path, init] = request.mock.calls[1]?.arguments ?? []
  assert.equal(path, '/api/tools/mcp/import')
  assert.deepEqual(JSON.parse(String(init?.body)), { ...connection, server_name: 'Team', name_prefix: 'team', tools: [tool] })
  assert.deepEqual(imported.tools, [{ id: 'created', name: 'team_lookup' }])
})

for (const tools of [[{ ...tool, inputSchema: [] }], [tool, tool], [{ name: 'lookup' }]]) {
  test('discovery rejects malformed or duplicated definitions', async (context) => {
    ownSession(context)
    context.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true, tools }))
    await assert.rejects(discoverMcpCatalog(connection), z.ZodError)
  })
}

test('imports reject overlength local or server names and invalid acknowledgements', async (context) => {
  ownSession(context)
  const request = context.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  await assert.rejects(importMcpCatalog({ ...connection, server_name: 'Team', name_prefix: 'a'.repeat(98), tools: [tool] }), z.ZodError)
  await assert.rejects(importMcpCatalog({ ...connection, server_name: 'a'.repeat(99), name_prefix: 'team', tools: [tool] }), z.ZodError)
  await assert.rejects(importMcpCatalog({ ...connection, server_url: `https://example.test/${'a'.repeat(500)}`, server_name: 'Team', name_prefix: 'team', tools: [tool] }), z.ZodError)
  assert.equal(request.mock.callCount(), 0)
  await assert.rejects(importMcpCatalog({ ...connection, server_name: 'Team', name_prefix: 'team', tools: [tool] }), z.ZodError)
})

test('discovery retains remote names longer than the local import limit for review', async (context) => {
  ownSession(context)
  const remote = { ...tool, name: 'a'.repeat(101) }
  context.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true, tools: [tool, remote] }))
  const inventory = await discoverMcpCatalog(connection)
  assert.deepEqual(inventory.tools.map((definition) => definition.name), [tool.name, remote.name])
})

for (const tools of [[], [{ id: 'a', name: 'other_lookup' }], [{ id: '', name: 'team_lookup' }], [{ id: 'a', name: 'team_lookup' }, { id: 'b', name: 'team_lookup' }]]) {
  test('atomic import refuses an acknowledgement inconsistent with the reviewed batch', async (context) => {
    ownSession(context)
    context.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true, tools }))
    await assert.rejects(importMcpCatalog({ ...connection, server_name: 'Team', name_prefix: 'team', tools: [tool] }), z.ZodError)
  })
}
