import { expect, test, type Page } from '@playwright/test'
import { ownedUploadBrowser } from './upload-policy-browser-fixture'
import { openUploads, prepareUploads, uploadLabel, uploadPolicy, type UploadSurface } from './upload-policy-fixture'
import { chatPath, firstSession, navigateChat, secondSession } from './chat-identity-fixture'
import { originalToken, replacementAgent, replacementToken, writeFromOtherTab } from './session-fencing-fixture'

if (process.env.BROWSER_SKILL_ROOT) test.use({ browser: ownedUploadBrowser })

async function replaceWorkspace(page: Page, surface: UploadSurface) {
  await writeFromOtherTab(page, replacementToken)
  await page.getByRole('link', { name: 'Agents', exact: true }).first().click()
  await page.getByRole('link', { name: new RegExp(replacementAgent.name) }).click()
  if (surface === 'workspace') await page.getByRole('link', { name: 'Files', exact: true }).click()
  else await expect(page).toHaveURL(chatPath(firstSession, replacementAgent).replace(`/${firstSession}`, ''))
}

test('chat never sends an upload completed after conversation retirement', async ({ page }) => {
  // Given
  const fixture = await prepareUploads(page)
  const requested = Promise.withResolvers<void>()
  const release = Promise.withResolvers<void>()
  const finished = Promise.withResolvers<void>()
  await page.route('**/api/chat/upload', async (route) => {
    requested.resolve()
    await release.promise
    try { await route.fulfill({ json: { filename: 'retired.txt', extracted_text: 'Retired conversation bytes', workspace_path: 'uploads/retired.txt' } }) }
    finally { finished.resolve() }
  })
  try {
    await openUploads(page, 'chat')
    await fixture.socket()
    await expect(page.getByLabel('Attach a file', { exact: true })).toBeEnabled()
    await page.getByLabel('Attach a file', { exact: true }).setInputFiles({ name: 'retired.txt', mimeType: 'text/plain', buffer: Buffer.from('private') })
    await requested.promise
    // When
    await navigateChat(page, chatPath(secondSession))
    await fixture.socket(1)
    release.resolve()
    await finished.promise
    // Then
    await expect(page.getByText('Retired conversation bytes', { exact: true })).toHaveCount(0)
    await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
    expect(fixture.sockets.flatMap((socket) => socket.sent)).toEqual([])
  } finally { release.resolve() }
})

for (const surface of ['chat', 'workspace'] as const) {
  for (const status of [200, 503]) {
    test(`${surface} discards a late policy ${status} from a retired session`, async ({ page }) => {
      // Given
      const fixture = await prepareUploads(page)
      const requested = Promise.withResolvers<void>()
      const release = Promise.withResolvers<void>()
      const finished = Promise.withResolvers<void>()
      await page.route('**/api/chat/upload-policy', async (route) => {
        if (route.request().headers().authorization !== `Bearer ${originalToken}`) {
          return route.fulfill({ status: 503, json: { detail: 'Current policy must be retried' } })
        }
        requested.resolve()
        await release.promise
        try { await route.fulfill({ status, json: uploadPolicy }) }
        finally { finished.resolve() }
      })
      try {
        await openUploads(page, surface)
        await requested.promise
        await expect(page.getByLabel(uploadLabel(surface), { exact: true })).toBeDisabled()
        // When
        await replaceWorkspace(page, surface)
        await expect(page.getByRole('button', { name: 'Retry upload limits' })).toBeVisible()
        release.resolve()
        await finished.promise
        // Then
        await expect(page.getByLabel(uploadLabel(surface), { exact: true })).toBeDisabled()
        expect(fixture.uploads).toEqual([])
        await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
      } finally { release.resolve() }
    })

    test(`${surface} discards a late upload ${status} from a retired session`, async ({ page }) => {
      // Given
      const fixture = await prepareUploads(page)
      const requested = Promise.withResolvers<void>()
      const release = Promise.withResolvers<void>()
      const finished = Promise.withResolvers<void>()
      await page.route(/\/api\/(chat\/upload|agents\/[^/]+\/files\/upload)(?:\?|$)/, async (route) => {
        requested.resolve()
        await release.promise
        try { await route.fulfill({ status, json: { filename: 'retired.txt', extracted_text: 'Private retired upload', workspace_path: 'uploads/retired.txt' } }) }
        finally { finished.resolve() }
      })
      try {
        await openUploads(page, surface)
        if (surface === 'chat') await fixture.socket()
        const input = page.getByLabel(uploadLabel(surface), { exact: true })
        await expect(input).toBeEnabled()
        await input.setInputFiles({ name: 'retired.txt', mimeType: 'text/plain', buffer: Buffer.from('private bytes') })
        await requested.promise
        // When
        await replaceWorkspace(page, surface)
        await expect(page.getByLabel(uploadLabel(surface), { exact: true })).toBeEnabled()
        release.resolve()
        await finished.promise
        // Then
        await expect(page.locator('[data-sonner-toast]')).toHaveCount(0)
        await expect(page.getByText('Private retired upload', { exact: true })).toHaveCount(0)
        expect(fixture.sockets.flatMap((socket) => socket.sent)).toEqual([])
      } finally { release.resolve() }
    })
  }
}
