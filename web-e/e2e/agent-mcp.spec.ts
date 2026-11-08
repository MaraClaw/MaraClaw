import { expect, test } from '@playwright/test'

import { originalAgent, prepareWorkspace } from './session-fencing-fixture'
import { mcpServer as summary } from './agent-mcp-data'

const path = `/app/agents/${originalAgent.id}/mcp`
const apiPath = `/api/agents/${originalAgent.id}/mcp/servers`

test('manager adds, configures and removes a server while validation failures preserve the draft', async ({ page }, testInfo) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  let created = false
  let fail = true
  const saving = Promise.withResolvers<void>()
  const releaseSave = Promise.withResolvers<void>()
  await page.route(`**${apiPath}`, async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { servers: created ? [summary] : [] } })
    expect(route.request().method()).toBe('POST')
    expect(route.request().postDataJSON()).toEqual({ name: 'research', transport: 'sse', enabled: true, url: 'https://mcp.example.test/events', headers: { Authorization: 'synthetic-secret' } })
    if (fail) { saving.resolve(); await releaseSave.promise; fail = false; return route.fulfill({ status: 422, json: { detail: 'This server configuration is invalid.' } }) }
    created = true
    return route.fulfill({ json: { ...summary, transport: 'sse' } })
  })
  await page.route(`**${apiPath}/research`, async (route) => {
    if (route.request().method() === 'PATCH') {
      expect(route.request().postDataJSON()).toEqual({ enabled: false, headers: {}, removed_headers: ['Authorization'] })
      return route.fulfill({ json: { ...summary, transport: 'sse', enabled: false, header_names: [] } })
    }
    expect(route.request().method()).toBe('DELETE')
    return route.fulfill({ status: 204 })
  })
  await page.goto(path)
  await expect(page.getByText('No MCP servers', { exact: true })).toBeVisible()
  const links = await page.getByRole('navigation', { name: 'Agent sections' }).getByRole('link').allTextContents()
  expect(links.indexOf('MCP')).toBe(links.indexOf('Tools') + 1)
  await page.getByRole('button', { name: 'Add server', exact: true }).click()
  await page.getByLabel('Server name', { exact: true }).fill('research')
  await page.getByLabel('Transport', { exact: true }).selectOption('sse')
  await page.getByLabel('Remote URL', { exact: true }).fill('https://mcp.example.test/events')
  await page.getByRole('button', { name: 'Add header', exact: true }).click()
  await page.getByLabel('Header name 1', { exact: true }).fill('Authorization')
  await page.getByLabel('Value 1', { exact: true }).fill('synthetic-secret')
  await page.getByRole('button', { name: 'Save server', exact: true }).click()
  await saving.promise
  await expect(page.getByRole('button', { name: 'Saving...', exact: true })).toBeDisabled()
  await expect(page.getByLabel('Remote URL', { exact: true })).toBeDisabled()
  await page.screenshot({ path: testInfo.outputPath('save-pending.png') })
  releaseSave.resolve()
  await expect(page.getByRole('alert')).toHaveText('This server configuration is invalid.')
  await expect(page.getByLabel('Value 1', { exact: true })).toHaveValue('synthetic-secret')
  await page.screenshot({ path: testInfo.outputPath('invalid-config-retained.png') })
  await page.getByRole('button', { name: 'Save server', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Add server', exact: true })).toBeFocused()
  await page.getByRole('button', { name: 'Configure research', exact: true }).click()
  await expect(page.getByLabel('Replacement value 1', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('Remote URL', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('Server name', { exact: true })).toHaveAttribute('readonly', '')
  await expect(page.getByLabel('Transport', { exact: true })).toBeDisabled()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Configure research', exact: true })).toBeFocused()
  await page.getByRole('button', { name: 'Configure research', exact: true }).click()
  await page.getByLabel('Enabled', { exact: true }).uncheck()
  await page.getByRole('button', { name: 'Remove header 1', exact: true }).click()
  await page.getByRole('button', { name: 'Save server', exact: true }).click()
  await expect(page.getByText('SSE · Disabled', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Remove research', exact: true }).click()
  await page.getByRole('button', { name: 'Remove server', exact: true }).click()
  await expect(page.getByText('No MCP servers', { exact: true })).toBeVisible()
})

test('use-only members see the access requirement without requesting server configuration', async ({ page }) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: { ...originalAgent, access_level: 'use' } }))
  const requests: string[] = []
  page.on('request', (request) => { if (request.url().includes('/mcp/servers')) requests.push(request.url()) })
  await page.goto(path)
  await expect(page.getByText('Manage access is required to view and configure this agent’s MCP servers.', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Add server', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /Configure|Remove/ })).toHaveCount(0)
  expect(requests).toEqual([])
})

test('MCP list errors offer retry and delayed requests show loading without an empty-state flash', async ({ page }) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  const release = Promise.withResolvers<void>()
  let recovered = false
  await page.route(`**${apiPath}`, async (route) => {
    if (!recovered) { await release.promise; return route.fulfill({ status: 503, json: { detail: 'Guest unavailable' } }) }
    return route.fulfill({ json: { servers: [summary] } })
  })
  try {
    await page.goto(path)
    await expect(page.getByText('Loading MCP servers...', { exact: true })).toBeVisible()
    await expect(page.getByText('No MCP servers', { exact: true })).toHaveCount(0)
    release.resolve()
    await expect(page.getByRole('alert')).toHaveText('Unable to load MCP servers. Check your connection and try again.')
    recovered = true
    await page.getByRole('button', { name: 'Retry servers', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'research', exact: true })).toBeVisible()
  } finally { release.resolve() }
})

test('stdio fields preserve separate arguments and allow environment fields to be removed', async ({ page }) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  await page.route(`**${apiPath}`, async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { servers: [] } })
    expect(route.request().postDataJSON()).toEqual({ name: 'local', transport: 'stdio', enabled: false, command: 'node', args: ['two words'], env: { TOKEN: 'synthetic-env' } })
    return route.fulfill({ json: { ...summary, name: 'local', transport: 'stdio', enabled: false, url_configured: false, command_configured: true, header_names: [], env_names: ['TOKEN'], args_count: 1 } })
  })
  await page.goto(path)
  await page.getByRole('button', { name: 'Add server', exact: true }).click()
  await page.getByLabel('Server name', { exact: true }).fill('local')
  await page.getByLabel('Transport', { exact: true }).selectOption('stdio')
  await page.getByLabel('Enabled', { exact: true }).uncheck()
  await page.getByLabel('Command', { exact: true }).fill('node')
  await page.getByRole('button', { name: 'Add argument', exact: true }).click()
  await page.getByLabel('Argument 1', { exact: true }).fill('two words')
  await page.getByRole('button', { name: 'Add argument', exact: true }).click()
  await page.getByRole('button', { name: 'Remove argument 2', exact: true }).click()
  await page.getByRole('button', { name: 'Add variable', exact: true }).click()
  await page.getByLabel('Variable name 1', { exact: true }).fill('TOKEN')
  await page.getByLabel('Value 1', { exact: true }).fill('synthetic-env')
  await page.getByRole('button', { name: 'Add variable', exact: true }).click()
  await page.getByRole('button', { name: 'Remove variable 2', exact: true }).click()
  await page.getByRole('button', { name: 'Save server', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'local', exact: true })).toBeVisible()
})

test('an offline save keeps its draft pending and resumes once the connection returns', async ({ page, context }, testInfo) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  let saved = false
  await page.route(`**${apiPath}`, (route) => route.fulfill({ json: { servers: [{ ...summary, enabled: !saved }] } }))
  await page.route(`**${apiPath}/research`, (route) => {
    expect(route.request().method()).toBe('PATCH')
    saved = true
    return route.fulfill({ json: { ...summary, enabled: false } })
  })
  await page.goto(path)
  await page.getByRole('button', { name: 'Configure research', exact: true }).click()
  await page.getByLabel('Enabled', { exact: true }).uncheck()
  try {
    await context.setOffline(true)
    await page.getByRole('button', { name: 'Save server', exact: true }).click()
    await expect(page.getByText('You’re offline. Saving will resume when the connection returns.', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Saving...', exact: true })).toBeDisabled()
    expect(saved).toBe(false)
    await page.screenshot({ path: testInfo.outputPath('offline-save.png') })
    await context.setOffline(false)
    await expect(page.getByText('HTTP · Disabled', { exact: true })).toBeVisible()
    expect(saved).toBe(true)
  } finally { await context.setOffline(false) }
})

test('a denied refresh retires cached manager data and an open credential editor', async ({ page, context }, testInfo) => {
  await prepareWorkspace(page)
  await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
  let denied = false
  await page.route(`**${apiPath}`, (route) => denied
    ? route.fulfill({ status: 403, json: { detail: 'Manage access required' } })
    : route.fulfill({ json: { servers: [summary] } }))
  await page.goto(path)
  await page.getByRole('button', { name: 'Configure research', exact: true }).click()
  denied = true
  await context.setOffline(true)
  await context.setOffline(false)
  await expect(page.getByText('Manage access is required to view and configure this agent’s MCP servers.', { exact: true })).toBeVisible()
  await expect(page.getByRole('list', { name: 'Installed MCP servers' })).toHaveCount(0)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Add server', exact: true })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('revoked-manage-access.png') })
})

for (const action of ['save', 'remove'] as const) {
  test(`a successful ${action} cannot clear a concurrent access denial before React renders`, async ({ page, context }) => {
    await prepareWorkspace(page)
    await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
    let denied = false
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    await page.route(`**${apiPath}`, (route) => denied
      ? route.fulfill({ status: 403, json: { detail: 'Manage access required' } })
      : route.fulfill({ json: { servers: [summary] } }))
    await page.route(`**${apiPath}/research`, async (route) => {
      requested.resolve()
      await release.promise
      return action === 'save' ? route.fulfill({ json: { ...summary, enabled: false } }) : route.fulfill({ status: 204 })
    })
    await page.goto(path)
    await page.getByRole('button', { name: action === 'save' ? 'Configure research' : 'Remove research', exact: true }).click()
    await page.getByRole('button', { name: action === 'save' ? 'Save server' : 'Remove server', exact: true }).click()
    await requested.promise
    await page.clock.install()
    await page.clock.pauseAt(new Date())
    denied = true
    try {
      const response = page.waitForResponse((value) => value.url().endsWith(apiPath) && value.status() === 403)
      await context.setOffline(true)
      await context.setOffline(false)
      await response
      const mutation = page.waitForResponse((value) => value.url().endsWith(`${apiPath}/research`) && value.status() < 300)
      release.resolve()
      await mutation
      await page.clock.runFor(100)
      await expect(page.getByText('Manage access is required to view and configure this agent’s MCP servers.', { exact: true })).toBeVisible()
      await expect(page.getByRole('list', { name: 'Installed MCP servers' })).toHaveCount(0)
      await expect(page.getByRole('dialog')).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Add server', exact: true })).toHaveCount(0)
    } finally { release.resolve(); await context.setOffline(false) }
  })

  test(`a stale list response cannot undo a successful ${action}`, async ({ page, context }) => {
    await prepareWorkspace(page)
    await page.route(`**/api/agents/${originalAgent.id}`, (route) => route.fulfill({ json: originalAgent }))
    let hold = false
    const requested = Promise.withResolvers<void>()
    const release = Promise.withResolvers<void>()
    const completed = Promise.withResolvers<void>()
    await page.route(`**${apiPath}`, async (route) => {
      if (hold) {
        requested.resolve()
        await release.promise
        try { await route.fulfill({ json: { servers: [summary] } }) } finally { completed.resolve() }
      } else await route.fulfill({ json: { servers: [summary] } })
    })
    await page.route(`**${apiPath}/research`, (route) => {
      expect(route.request().method()).toBe(action === 'save' ? 'PATCH' : 'DELETE')
      return action === 'save' ? route.fulfill({ json: { ...summary, enabled: false } }) : route.fulfill({ status: 204 })
    })
    await page.goto(path)
    await page.getByRole('button', { name: action === 'save' ? 'Configure research' : 'Remove research', exact: true }).click()
    hold = true
    try {
      await context.setOffline(true)
      await context.setOffline(false)
      await requested.promise
      if (action === 'save') {
        await page.getByLabel('Enabled', { exact: true }).uncheck()
        await page.getByRole('button', { name: 'Save server', exact: true }).click()
      } else await page.getByRole('button', { name: 'Remove server', exact: true }).click()
      await expect(page.getByRole('dialog')).toHaveCount(0)
      release.resolve()
      await completed.promise
      await expect(page.getByText(action === 'save' ? 'HTTP · Disabled' : 'No MCP servers', { exact: true })).toBeVisible()
    } finally { release.resolve(); await context.setOffline(false) }
  })
}
