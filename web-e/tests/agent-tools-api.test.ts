import assert from 'node:assert/strict'
import test from 'node:test'
import { z } from 'zod'
import { patchAgentToolConfig, probeAgentTool, saveAgentToolConfig } from '../src/lib/agent-tools-api.ts'

test('agent additive save transports explicit operations separately from legacy replacement', async (context) => {
  const requests: { readonly path: string; readonly init: RequestInit }[] = []
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => { requests.push({ path, init }); return Response.json({ ok: true }) })
  await patchAgentToolConfig('agent/a', 'tool/b', { set: { region: 'eu' }, unset: ['api_key'] })
  await saveAgentToolConfig('agent/a', 'tool/b', { region: 'us' })
  assert.equal(requests[0]?.path, '/api/tools/agents/agent%2Fa/tool-config/tool%2Fb')
  assert.equal(requests[0]?.init.method, 'PATCH')
  assert.deepEqual(JSON.parse(String(requests[0]?.init.body)), { set: { region: 'eu' }, unset: ['api_key'] })
  assert.equal(requests[1]?.init.method, 'PUT')
  assert.deepEqual(JSON.parse(String(requests[1]?.init.body)), { config: { region: 'us' } })
})

test('member API refuses privileged set and unset operations before transport', async (context) => {
  const request = context.mock.method(globalThis, 'fetch', async () => Response.json({ ok: true }))
  for (const key of ['allow_network', 'http_proxy', 'https_proxy', 'no_proxy']) {
    await assert.rejects(patchAgentToolConfig('agent', 'tool', { set: { [key]: 'value' }, unset: [] }), z.ZodError)
    await assert.rejects(patchAgentToolConfig('agent', 'tool', { set: {}, unset: [key] }), z.ZodError)
  }
  assert.equal(request.mock.callCount(), 0)
})

test('saved agent probe references its original agent and forwards only draft operations', async (context) => {
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/tools/test-email')
    assert.equal(init.signal, controller.signal)
    assert.deepEqual(JSON.parse(String(init.body)), { saved: { agent_id: 'agent', tool_id: 'tool' }, operations: { set: { smtp_port: 587 }, unset: [] } })
    return Response.json({ ok: true, message: 'Connected' })
  })
  assert.deepEqual(await probeAgentTool({ agentId: 'agent', toolId: 'tool', kind: 'email' }, { set: { smtp_port: 587 }, unset: [] }, controller.signal), { ok: true })
})
