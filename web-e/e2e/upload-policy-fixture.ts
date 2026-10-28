import { expect, type Page } from '@playwright/test'
import { prepareChat, chatPath } from './chat-identity-fixture'
import { originalAgent } from './session-fencing-fixture'
import { uploadPolicy } from './upload-policy-data'
export { uploadPolicy } from './upload-policy-data'

export type UploadSurface = 'chat' | 'workspace'
export const uploadPath = (surface: UploadSurface) => surface === 'chat' ? chatPath() : `/app/agents/${originalAgent.id}/files`
export const uploadLabel = (surface: UploadSurface) => surface === 'chat' ? 'Attach a file' : 'Upload workspace file'

export async function prepareUploads(page: Page) {
  const chat = await prepareChat(page)
  await page.route('**/api/chat/upload-policy', (route) => {
    expect(route.request().method()).toBe('GET')
    expect(route.request().headers().authorization).toMatch(/^Bearer fencing-(original|replacement)-session$/)
    return route.fulfill({ json: uploadPolicy })
  })
  await page.route('**/api/agents/*/files/**', (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: [] })
    return route.fallback()
  })
  const uploads: { readonly path: string }[] = []
  await page.route(/\/api\/(chat\/upload|agents\/[^/]+\/files\/upload)(?:\?|$)/, async (route) => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().headers()['content-type']).toMatch(/^multipart\/form-data; boundary=/)
    expect(route.request().headers().authorization).toMatch(/^Bearer fencing-(original|replacement)-session$/)
    uploads.push({ path: new URL(route.request().url()).pathname })
    await route.fulfill({ json: { filename: 'accepted.txt', extracted_text: 'Accepted upload', workspace_path: 'uploads/accepted.txt' } })
  })
  return { ...chat, uploads }
}

export async function openUploads(page: Page, surface: UploadSurface) {
  await page.goto(uploadPath(surface))
  await expect(page.locator('input[type="file"]')).toBeVisible()
}

export async function selectNativeUploadFile(page: Page, filePath: string) {
  const input = page.locator('input[type="file"]')
  await input.evaluate((element) => element.addEventListener('change', () => {
    if (element instanceof HTMLInputElement) element.dataset.uploadObservedSize = String(element.files?.[0]?.size)
  }, { once: true, capture: true }))
  const cdp = await page.context().newCDPSession(page)
  try {
    const { root } = await cdp.send('DOM.getDocument')
    const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: 'input[type="file"]' })
    await cdp.send('DOM.setFileInputFiles', { nodeId, files: [filePath] })
    return Number(await input.getAttribute('data-upload-observed-size'))
  } finally { await cdp.detach() }
}
