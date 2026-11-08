import { expect, test } from '@playwright/test'

import { mcpServer } from './agent-mcp-data'
import { originalAgent, prepareWorkspace } from './session-fencing-fixture'
import { MCPServerPatchSchema } from '../src/lib/engine-contracts.generated'

const path = `/app/agents/${originalAgent.id}/mcp`
const apiPath = `/api/agents/${originalAgent.id}/mcp/servers`

test('redacted stdio editing preserves saved command and arguments, explicitly clears argv, then replaces it', async ({ page }, testInfo) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  let server = { ...mcpServer, transport: 'stdio', url_configured: false, command_configured: true, args_count: 2, header_names: [], env_names: ['TOKEN'] }
  let saves = 0
  await page.route(`**${apiPath}`, (route) => route.fulfill({ json: { servers: [server] } }))
  await page.route(`**${apiPath}/research`, (route) => {
    expect(route.request().method()).toBe('PATCH')
    const body = MCPServerPatchSchema.parse(route.request().postDataJSON())
    const common = { enabled: false, env: {}, removed_env: [] }
    switch (saves++) {
      case 0: expect(body).toEqual(common); server = { ...server, enabled: false }; break
      case 1: expect(body).toEqual({ ...common, args: [] }); server = { ...server, args_count: 0 }; break
      case 2: expect(body).toEqual({ ...common, command: 'node', args: ['--workspace', 'two words'] }); server = { ...server, args_count: 2 }; break
      default: throw new Error('Unexpected MCP save')
    }
    return route.fulfill({ json: server })
  })
  await page.goto(path)
  await page.getByRole('button', { name: 'Configure research', exact: true }).click()
  await expect(page.getByLabel('Command', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('Transport', { exact: true })).toBeDisabled()
  await expect(page.getByText('2 saved arguments. Values are hidden.', { exact: true })).toBeVisible()
  await expect(page.getByLabel('Replacement value 1', { exact: true })).toHaveValue('')
  await expect(page.getByRole('button', { name: 'Add argument', exact: true })).toHaveCount(0)
  await page.getByLabel('Enabled', { exact: true }).uncheck()
  await page.screenshot({ path: testInfo.outputPath('stdio-preserve-redacted.png') })
  await page.getByRole('button', { name: 'Save server', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.getByRole('button', { name: 'Configure research', exact: true }).click()
  await page.getByLabel('Replace arguments', { exact: true }).check()
  await expect(page.getByText('The replacement list overwrites all saved arguments. An empty list clears them.', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('stdio-clear-arguments.png') })
  await page.getByRole('button', { name: 'Save server', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.getByRole('button', { name: 'Configure research', exact: true }).click()
  await expect(page.getByText('0 saved arguments. Values are hidden.', { exact: true })).toBeVisible()
  await page.getByLabel('Command', { exact: true }).fill('node')
  await page.getByLabel('Replace arguments', { exact: true }).check()
  await page.getByRole('button', { name: 'Add argument', exact: true }).click()
  await page.getByLabel('Argument 1', { exact: true }).fill('--workspace')
  await page.getByRole('button', { name: 'Add argument', exact: true }).click()
  await page.getByLabel('Argument 2', { exact: true }).fill('two words')
  await page.getByRole('button', { name: 'Save server', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(saves).toBe(3)
})

for (const failure of [
  { status: 409, detail: 'OpenClaw must use hybrid hot reload for MCP configuration.', message: 'OpenClaw must use hybrid hot reload for MCP configuration.' },
  { status: 503, detail: { code: 'unknown_sync_failure', saved: true, sync_error: 'private-credential-payload' }, message: 'Unable to save this server. Check your connection and try again.' },
] as const) {
test(`an uncommitted backend ${failure.status} retains replacement details without claiming persistence or connectivity`, async ({ page }, testInfo) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  await page.route(`**${apiPath}`, (route) => route.fulfill({ json: { servers: [mcpServer] } }))
  await page.route(`**${apiPath}/research`, (route) => {
    expect(MCPServerPatchSchema.parse(route.request().postDataJSON())).toEqual({ enabled: true, url: 'https://mcp.example.test/new', headers: {}, removed_headers: [] })
    return route.fulfill({ status: failure.status, json: { detail: failure.detail } })
  })
  await page.goto(path)
  await expect(page.getByText('Stored configurations are synced to OpenClaw. A missing guest configuration does not remove your stored servers.', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Configure research', exact: true }).click()
  await expect(page.getByLabel('Remote URL', { exact: true })).toHaveValue('')
  await page.getByLabel('Remote URL', { exact: true }).fill('https://mcp.example.test/new')
  await page.getByRole('button', { name: 'Save server', exact: true }).click()
  await expect(page.getByRole('alert')).toHaveText(failure.message)
  await expect(page.getByText('private-credential-payload', { exact: false })).toHaveCount(0)
  await expect(page.getByLabel('Remote URL', { exact: true })).toHaveValue('https://mcp.example.test/new')
  await page.screenshot({ path: testInfo.outputPath('sync-error-retains-draft.png') })
})
}
