import { expect, type Page } from '@playwright/test'
import { originalAgent, prepareWorkspace } from './session-fencing-fixture'

export const toolId = 'ca55816c-ab1a-46ba-aa1f-01bb09d8fed7'
export const toolsPath = `/app/agents/${originalAgent.id}/tools`
export const tool = {
  id: toolId, name: 'synthetic_lookup', display_name: 'Synthetic shared MCP connector', description: 'Synthetic connector settings', type: 'mcp', category: 'integration', icon: 'tool',
  enabled: true, is_default: false, source: 'admin', mcp_server_url: 'https://mcp.example.test/mcp', mcp_server_name: 'Synthetic', agent_tool_id: null,
  config_schema: { fields: [{ key: 'api_key', label: 'API key', type: 'password' }, { key: 'region', label: 'Region', type: 'text' }, { key: 'http_proxy', label: 'Privileged proxy', type: 'text' }] },
  global_config: { api_key: '********', region: 'eu-global', http_proxy: '********' }, agent_config: { api_key: '********', region: 'us-agent' },
}

export async function prepareAgentTools(page: Page) {
  await prepareWorkspace(page)
  let overrides: Record<string, unknown> = { ...tool.agent_config }
  const writes: unknown[] = []
  const probes: unknown[] = []
  const state = { status: 200, writes, probes }
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  await page.route(`**/api/agents/${originalAgent.id}/metrics`, (route) => route.fulfill({ json: { status: 'stopped', tasks: { total: 0, done: 0, pending: 0 } } }))
  await page.route('**/api/tools/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/with-config')) return route.fulfill({ json: [{ ...tool, agent_config: overrides }] })
    if (path === '/api/tools/test-mcp') {
      probes.push(request.postDataJSON())
      return route.fulfill({ json: { ok: true, tools: [] } })
    }
    expect(path).toBe(`/api/tools/agents/${originalAgent.id}/tool-config/${toolId}`)
    if (request.method() === 'GET') return route.fulfill({ json: { global_config: tool.global_config, agent_config: overrides, merged_config: { ...tool.global_config, ...overrides }, config_schema: tool.config_schema } })
    expect(request.method()).toBe('PATCH')
    const body: { set: Record<string, unknown>; unset: string[] } = request.postDataJSON()
    expect(JSON.stringify(body)).not.toContain('****')
    writes.push(body)
    if (state.status !== 200) return route.fulfill({ status: state.status, json: { detail: 'Synthetic write failure' } })
    overrides = { ...Object.fromEntries(Object.entries(overrides).filter(([key]) => !body.unset.includes(key))), ...body.set }
    return route.fulfill({ json: { ok: true } })
  })
  return state
}
