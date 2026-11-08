import { expect, type Page } from '@playwright/test'
import type { ChatMessage } from '../src/lib/chat/message'

export async function expectVirtualTranscript(page: Page, expected: readonly ChatMessage[]) {
  const expectedIds = expected.map((row) => row.entry_id)
  const mountedRows = () => page.locator('[data-chat-entry]').evaluateAll((elements) => elements.map((element) => ({
    id: element.getAttribute('data-chat-entry'), content: element.querySelector('[data-chat-content]')?.textContent,
  })))
  await expect.poll(async () => {
    const mounted = await mountedRows()
    return mounted.length > 0 && mounted.length <= 40 && mounted.every((row) => {
      const index = expectedIds.indexOf(row.id ?? undefined)
      return index >= 0 && row.content === expected[index]?.content
    })
  }).toBe(true)
  const thread = page.locator('main [aria-busy]')
  await thread.evaluate((element) => { element.scrollTop = element.scrollHeight; element.dispatchEvent(new Event('scroll')) })
  await expect(page.locator(`[data-chat-entry="${expectedIds.at(-1)}"]`)).toHaveCount(1)
  const seen = new Map<string, { readonly content: string | null | undefined; readonly top: number }>()
  await thread.evaluate((element) => { element.scrollTop = 200; element.dispatchEvent(new Event('scroll')) })
  for (;;) {
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
    const mounted = await thread.evaluate((element) => {
      const top = element.getBoundingClientRect().top
      return [...element.querySelectorAll<HTMLElement>('[data-chat-entry]')].map((row) => ({
        id: row.dataset.chatEntry ?? '', content: row.querySelector('[data-chat-content]')?.textContent,
        top: row.getBoundingClientRect().top - top + element.scrollTop,
      }))
    })
    expect(mounted.length).toBeLessThanOrEqual(40)
    const indices = mounted.map((row) => expectedIds.indexOf(row.id))
    expect(indices.every((index, position) => index >= 0 && (position === 0 || index > (indices[position - 1] ?? -1)))).toBe(true)
    for (const row of mounted) seen.set(row.id, row)
    const advanced = await thread.evaluate((element) => {
      const before = element.scrollTop
      element.scrollTop += element.clientHeight / 2
      element.dispatchEvent(new Event('scroll'))
      return element.scrollTop > before
    })
    if (!advanced) break
  }
  expect([...seen.keys()].sort()).toEqual([...expectedIds].sort())
  for (const row of expected) expect(seen.get(row.entry_id ?? '')?.content).toBe(row.content)
}
