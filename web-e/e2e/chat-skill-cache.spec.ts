import { expect, test } from '@playwright/test'
import { chatPath, navigateChat, submitted } from './chat-identity-fixture'
import { prepareChatSkills } from './chat-skill-mentions-fixture'
import { originalAgent, replacementAgent, replacementToken, writeFromOtherTab } from './session-fencing-fixture'
import { discoveryKey, folderNames, prepareDiscovery } from './workspace-discovery-fixture'

const composerName = `Message ${originalAgent.name}`

test('fresh repeated openings, filtering and returning to an agent reuse installed Files discovery', async ({ page }) => {
  // Given
  const fixture = await prepareChatSkills(page)
  await page.goto(chatPath())
  const composer = page.getByRole('textbox', { name: composerName })
  const picker = page.getByRole('region', { name: 'Installed skills' })
  await composer.fill('@')
  await expect(picker.getByRole('button')).toHaveCount(3)
  expect(fixture.requests).toHaveLength(4)
  // When
  await composer.press('Escape')
  await composer.fill('@rep')
  await expect(picker.getByRole('button')).toHaveCount(1)
  await composer.press('Escape')
  await composer.fill('@')
  await expect(picker.getByRole('button')).toHaveCount(3)
  await page.getByText('Installed skills', { exact: true }).click()
  await composer.click()
  await navigateChat(page, chatPath(undefined, replacementAgent))
  await page.getByRole('textbox', { name: `Message ${replacementAgent.name}` }).fill('@')
  await expect(picker.getByRole('button', { name: /^@replacement-only / })).toBeVisible()
  await navigateChat(page, chatPath())
  await composer.fill('@')
  // Then
  await expect(picker.getByRole('button')).toHaveCount(3)
  expect(fixture.requests.filter((request) => request.startsWith(`/api/agents/${originalAgent.id}/`))).toHaveLength(4)
  expect(fixture.requests.filter((request) => request.startsWith(`/api/agents/${replacementAgent.id}/`))).toHaveLength(1)
})

test('stale reopening refreshes once and keeps cached choices selectable during the background request', async ({ page }) => {
  // Given
  await page.clock.install()
  const fixture = await prepareChatSkills(page)
  await page.goto(chatPath())
  const composer = page.getByRole('textbox', { name: composerName })
  const picker = page.getByRole('region', { name: 'Installed skills' })
  await composer.fill('@')
  await expect(picker.getByRole('button')).toHaveCount(3)
  await composer.press('Escape')
  await composer.press('Backspace')
  await page.clock.fastForward(61_000)
  fixture.install('cache-addition')
  const gate = fixture.holdNextList()
  // When
  await composer.fill('@')
  await gate.requested
  try {
    await expect(picker.getByRole('button')).toHaveCount(3)
    await expect(picker.getByText('Loading installed skills…', { exact: true })).toHaveCount(0)
    await composer.fill('@aud')
    await expect(picker.getByRole('button', { name: /^@audit-review / })).toBeVisible()
    await composer.press('Enter')
    await expect(composer).toHaveValue('@audit-review ')
  } finally { gate.release() }
  await composer.fill('@')
  // Then
  await expect(picker.getByRole('button')).toHaveCount(4)
  expect(fixture.requests.filter((request) => request.endsWith(':skills'))).toHaveLength(2)
})

test('a failed background refresh keeps cached mentions available for sending', async ({ page }) => {
  // Given
  await page.clock.install()
  const fixture = await prepareChatSkills(page)
  await page.goto(chatPath())
  const socket = await fixture.chat.socket()
  const composer = page.getByRole('textbox', { name: composerName })
  const picker = page.getByRole('region', { name: 'Installed skills' })
  await composer.fill('@')
  await expect(picker.getByRole('button')).toHaveCount(3)
  await composer.press('Escape')
  await composer.press('Backspace')
  await page.clock.fastForward(61_000)
  fixture.setStatus(503)
  await composer.fill('@')
  await expect(picker.getByText(/Could not refresh installed skills/)).toBeVisible()
  // When
  await picker.getByRole('button', { name: /^@audit-review / }).click()
  await composer.press('Enter')
  // Then
  await expect.poll(() => socket.sent.length).toBe(1)
  expect(submitted(socket).content).toContain('skills/audit-review/SKILL.md')
  expect(submitted(socket).display_content).toBe('@audit-review')
})

for (const mutation of ['install', 'delete'] as const) {
  test(`${mutation} invalidates fresh installed skills through the existing agent Files prefix`, async ({ page }) => {
    // Given
    const fixture = await prepareChatSkills(page)
    await page.goto(chatPath())
    const composer = page.getByRole('textbox', { name: composerName })
    const picker = page.getByRole('region', { name: 'Installed skills' })
    await composer.fill('@')
    await expect(picker.getByRole('button')).toHaveCount(3)
    // When
    switch (mutation) {
      case 'install':
        await page.getByRole('link', { name: 'Skills', exact: true }).click()
        await page.getByRole('button', { name: 'Install', exact: true }).click()
        await expect(page.getByText('Skill copied into the agent workspace', { exact: true })).toBeVisible()
        break
      case 'delete':
        await page.getByRole('link', { name: 'Files', exact: true }).click()
        await page.getByRole('button', { name: '▸ skills', exact: true }).click()
        await page.getByRole('button', { name: 'report.md', exact: true }).click()
        await page.getByRole('button', { name: 'Delete', exact: true }).click()
        await expect(page.getByText('Select a file to read or edit.', { exact: true })).toBeVisible()
        break
    }
    await navigateChat(page, chatPath())
    await composer.fill('@')
    // Then
    await expect(picker.getByRole('button')).toHaveCount(mutation === 'install' ? 4 : 2)
    await expect(picker.getByRole('button', { name: mutation === 'install' ? /^@cache-addition / : /^@report / })).toHaveCount(mutation === 'install' ? 1 : 0)
    expect(fixture.requests.filter((request) => request === `/api/agents/${originalAgent.id}/files/:skills`).length).toBeGreaterThanOrEqual(2)
  })
}

for (const interruption of ['navigation', 'replacement'] as const) {
  test(`chat installed skill ${interruption} aborts four active folders without admitting the queue`, async ({ page }, info) => {
    const discovery = await prepareDiscovery(page)
    const holds = folderNames.slice(0, 4).map((name) => discovery.hold(discoveryKey('files', `skills/${name}`)))
    try {
      await page.goto(chatPath())
      const composer = page.getByRole('textbox', { name: composerName })
      await composer.fill('@')
      const requests = await Promise.all(holds.map((hold) => hold.requested))
      expect(discovery.requests.filter(({ key }) => key.includes(':skills/'))).toHaveLength(4)
      const aborted = requests.map((request) => page.waitForEvent('requestfailed', { predicate: (failed) => failed === request }))
      if (interruption === 'navigation') await navigateChat(page, '/app/agents')
      else await writeFromOtherTab(page, replacementToken)
      await Promise.all(aborted)
      await Promise.all(holds.map((hold) => hold.release()))
      expect(discovery.requests.filter(({ key }) => key.includes(':skills/'))).toHaveLength(4)
      const agent = interruption === 'navigation' ? originalAgent : replacementAgent
      await navigateChat(page, chatPath(undefined, agent))
      const activeComposer = page.getByRole('textbox', { name: `Message ${agent.name}` })
      await activeComposer.fill('@')
      const picker = page.getByRole('region', { name: 'Installed skills' })
      const name = interruption === 'navigation' ? 'folder-00' : 'current-only'
      await expect(picker.getByRole('button', { name: new RegExp(`^@${name} `) })).toBeVisible()
      await expect(picker.getByRole('button')).toHaveCount(interruption === 'navigation' ? folderNames.length + 1 : 1)
      await picker.getByRole('button', { name: new RegExp(`^@${name} `) }).click()
      await activeComposer.press('Enter')
      await expect.poll(() => discovery.chat.sockets.flatMap((socket) => socket.sent).length).toBe(1)
      const socket = discovery.chat.sockets.find((socket) => socket.sent.length > 0)
      expect(socket).toBeDefined()
      if (!socket) throw new Error('Expected submitted skill socket')
      expect(submitted(socket).content).toContain(interruption === 'navigation' ? 'skills/folder-00/SKILL.md' : 'skills/current-only.md')
      await page.screenshot({ path: info.outputPath(`chat-${interruption}.png`) })
    } finally { discovery.releaseAll() }
  })
}
