import { expect, type Page } from '@playwright/test'
import type { FileInfo } from '../src/lib/workspace-api'
import { prepareChat } from './chat-identity-fixture'
import { originalAgent, originalToken, replacementAgent } from './session-fencing-fixture'

const file = (path: string, is_dir = false): FileInfo => ({ name: path.split('/').at(-1) ?? '', path, is_dir, size: 0 })

export async function prepareChatSkills(page: Page) {
  const chat = await prepareChat(page)
  const files = new Map<string, readonly FileInfo[]>([
    ['', [file('skills', true)]],
    ['skills', [file('skills/audit-review', true), file('skills/quick-start', true), file('skills/report.md'),
      file('skills/.private', true), file('skills/.hidden.md'), file('skills/no-entry', true)]],
    ['skills/audit-review', [file('skills/audit-review/SKILL.md')]],
    ['skills/quick-start', [file('skills/quick-start/skill.md')]],
    ['skills/no-entry', [file('skills/no-entry/README.md')]],
  ])
  const requests: string[] = []
  const install = (name: string) => {
    files.set('skills', [...files.get('skills') ?? [], file(`skills/${name}`, true)])
    files.set(`skills/${name}`, [file(`skills/${name}/SKILL.md`)])
  }
  let status = 200
  let delayed: { readonly requested: ReturnType<typeof Promise.withResolvers<void>>; readonly released: ReturnType<typeof Promise.withResolvers<void>> } | undefined
  await page.route('**/api/agents/*/files/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    if (url.pathname === `/api/agents/${originalAgent.id}/files/import-skill`) {
      expect(request.method()).toBe('POST')
      expect(request.postDataJSON()).toEqual({ skill_id: 'cache-addition' })
      install('cache-addition')
      return route.fulfill({ json: { status: 'ok' } })
    }
    const path = url.searchParams.get('path') ?? ''
    expect([...url.searchParams.keys()]).toEqual(path ? ['path'] : [])
    requests.push(`${url.pathname}:${path}`)
    if (url.pathname === `/api/agents/${originalAgent.id}/files/content`) {
      expect(path).toBe('skills/report.md')
      if (request.method() === 'DELETE') {
        files.set('skills', (files.get('skills') ?? []).filter((entry) => entry.path !== path))
        return route.fulfill({ status: 204 })
      }
      expect(request.method()).toBe('GET')
      return route.fulfill({ json: { path, content: '# Report skill', version_token: 'report-v1' } })
    }
    expect(request.method()).toBe('GET')
    if (url.pathname === `/api/agents/${originalAgent.id}/files/revisions`) {
      expect(path).toBe('skills/report.md')
      return route.fulfill({ json: [] })
    }
    if (url.pathname === `/api/agents/${replacementAgent.id}/files/`) {
      expect(path).toBe('skills')
      return route.fulfill({ json: [file('skills/replacement-only.md')] })
    }
    expect(url.pathname).toBe(`/api/agents/${originalAgent.id}/files/`)
    const gate = path === 'skills' ? delayed : undefined
    if (gate) {
      delayed = undefined
      gate.requested.resolve()
      await gate.released.promise
    }
    if (path === 'skills' && status !== 200) return route.fulfill({ status, json: { detail: 'Installed files unavailable' } })
    expect(files.has(path), `Unexpected installed Files path: ${path}`).toBe(true)
    return route.fulfill({ json: files.get(path) })
  })
  await page.route('**/api/skills/**', async (route) => {
    const request = route.request()
    expect(request.method()).toBe('GET')
    expect(request.headers().authorization).toBe(`Bearer ${originalToken}`)
    const url = new URL(request.url())
    expect(url.search).toBe('')
    switch (url.pathname) {
      case '/api/skills/': return route.fulfill({ json: [{ id: 'cache-addition', name: 'Cache addition' }] })
      case '/api/skills/browse/list': return route.fulfill({ json: [] })
      default: throw new Error(`Unexpected skill catalog endpoint: ${url.pathname}`)
    }
  })
  return {
    chat, requests,
    clear: () => { files.set('skills', []) },
    setStatus: (next: number) => { status = next },
    install,
    holdNextList: () => {
      const gate = { requested: Promise.withResolvers<void>(), released: Promise.withResolvers<void>() }
      delayed = gate
      return { requested: gate.requested.promise, release: () => gate.released.resolve() }
    },
  }
}
