import { expect, test } from '@playwright/test'

import { mcpServer } from './agent-mcp-data'
import { originalAgent, prepareWorkspace } from './session-fencing-fixture'
import { MCPServerCreateSchema, MCPServerPatchSchema } from '../src/lib/engine-contracts.generated'

const path = `/app/agents/${originalAgent.id}/mcp`
const apiPath = `/api/agents/${originalAgent.id}/mcp/servers`

for (const operation of ['create', 'configure', 'delete'] as const) {
  test(`a committed 503 ${operation} refreshes saved inventory and closes the form without retrying`, async ({ page }, testInfo) => {
    await prepareWorkspace(page)
    await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
    let servers = operation === 'create' ? [] : [mcpServer]
    const writes: string[] = []
    let reads = 0
    await page.route(`**${apiPath}{,/**}`, (route) => {
      const method = route.request().method()
      if (method === 'GET') { reads++; return route.fulfill({ json: { servers } }) }
      writes.push(method)
      switch (operation) {
        case 'create':
          expect(method).toBe('POST')
          expect(MCPServerCreateSchema.parse(route.request().postDataJSON())).toMatchObject({ name: 'research', url: 'https://mcp.example.test/saved' })
          servers = [mcpServer]
          break
        case 'configure':
          expect(method).toBe('PATCH')
          expect(MCPServerPatchSchema.parse(route.request().postDataJSON())).toEqual({ enabled: false, headers: {}, removed_headers: ['Authorization'] })
          servers = [{ ...mcpServer, enabled: false, header_names: [] }]
          break
        case 'delete':
          expect(method).toBe('DELETE')
          servers = []
          break
        default: operation satisfies never
      }
      return route.fulfill({ status: 503, json: { detail: { code: 'mcp_saved_sync_pending', saved: true, sync_error: 'mcp_config_write_failed', credentials: 'must-not-appear' } } })
    })
    await page.goto(path)
    switch (operation) {
      case 'create':
        await page.getByRole('button', { name: 'Add server', exact: true }).click()
        await page.getByLabel('Server name', { exact: true }).fill('research')
        await page.getByLabel('Remote URL', { exact: true }).fill('https://mcp.example.test/saved')
        break
      case 'configure':
        await page.getByRole('button', { name: 'Configure research', exact: true }).click()
        await page.getByLabel('Enabled', { exact: true }).uncheck()
        await page.getByRole('button', { name: 'Remove header 1', exact: true }).click()
        break
      case 'delete':
        await page.getByRole('button', { name: 'Remove research', exact: true }).click()
        break
      default: operation satisfies never
    }
    reads = 0
    await page.getByRole('button', { name: operation === 'delete' ? 'Remove server' : 'Save server', exact: true }).click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.getByRole('status')).toHaveText(operation === 'delete' ? 'Server removed. Sync to OpenClaw is pending.' : 'Server saved. Sync to OpenClaw is pending.')
    await expect.poll(() => reads).toBe(1)
    if (operation === 'delete') await expect(page.getByText('No MCP servers', { exact: true })).toBeVisible()
    else await expect(page.getByRole('button', { name: 'Configure research', exact: true })).toBeVisible()
    if (operation === 'configure') {
      await expect(page.getByText('HTTP · Disabled', { exact: true })).toBeVisible()
      await expect(page.getByText('Headers: Authorization', { exact: true })).toHaveCount(0)
    }
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByText('must-not-appear', { exact: false })).toHaveCount(0)
    expect(writes).toEqual([operation === 'create' ? 'POST' : operation === 'configure' ? 'PATCH' : 'DELETE'])
    await page.screenshot({ path: testInfo.outputPath(`${operation}-saved-sync-pending.png`) })
  })
}
