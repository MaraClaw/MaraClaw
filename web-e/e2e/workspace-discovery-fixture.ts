import { expect, type Page, type Request } from '@playwright/test'
import { prepareChat } from './chat-identity-fixture'
import { tool } from './agent-tools-fixture'
import { originalAgent, originalToken, replacementAgent, replacementToken } from './session-fencing-fixture'

export const agentPath = (tab: string, agentId = originalAgent.id) => `/app/agents/${agentId}/${tab}`
export const discoveryKey = (kind: string, path = '', agentId = originalAgent.id) => `${kind}:${agentId}:${path}`
const file = (path: string, is_dir = false) => ({ name: path.split('/').at(-1), path, is_dir, size: 0 })
export const folderNames = Array.from({ length: 17 }, (_, index) => `folder-${String(index).padStart(2, '0')}`)

export async function prepareDiscovery(page: Page) {
  const chat = await prepareChat(page)
  const requests: { readonly key: string; readonly request: Request }[] = []
  const waiting = new Map<string, ReturnType<typeof createHold>>()
  const holds: ReturnType<typeof createHold>[] = []
  function createHold() {
    return { requested: Promise.withResolvers<Request>(), release: Promise.withResolvers<void>(), finished: Promise.withResolvers<void>() }
  }
  await page.route(/\/api\/(?:agents\/[^/]+\/files\/|pages\/list|tools\/agents\/[^/]+\/with-config)/, async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const token = request.headers()['authorization']
    expect([`Bearer ${originalToken}`, `Bearer ${replacementToken}`]).toContain(token)
    expect(request.method()).toBe('GET')
    const current = token === `Bearer ${replacementToken}`
    const agentId = url.pathname.match(/\/agents\/([^/]+)/)?.[1] ?? url.searchParams.get('agent_id')
    expect([originalAgent.id, replacementAgent.id]).toContain(agentId)
    const path = url.searchParams.get('path') ?? ''
    const kind = url.pathname.includes('/files/') ? 'files' : url.pathname.includes('/tools/') ? 'tools' : 'pages'
    const key = discoveryKey(kind, path, agentId ?? '')
    requests.push({ key, request })
    const hold = waiting.get(key)
    if (hold) {
      waiting.delete(key)
      hold.requested.resolve(request)
      await hold.release.promise
    }
    try {
      const owner = current ? 'current-owner' : 'old-owner'
      if (kind === 'pages') return await route.fulfill({ json: [{ id: `${owner}-page`, short_id: owner, source_path: 'workspace/site.html', title: `${owner} page`, view_count: 3, created_at: '2030-01-01T00:00:00Z', url: `/p/${owner}` }] })
      if (kind === 'tools') return await route.fulfill({ json: [{ ...tool, name: 'agentbay_browser_navigate', display_name: `${owner} browser tool` }] })
      if (path === 'skills') return await route.fulfill({ json: current ? [file('skills/current-only.md')] : [...folderNames.map((name) => file(`skills/${name}`, true)), file('skills/folder-00.md'), file('skills/.private', true), file('skills/flat.md')] })
      if (path.startsWith('skills/')) {
        expect(folderNames).toContain(path.slice('skills/'.length))
        return await route.fulfill({ json: [file(`${path}/skill.md`), file(`${path}/SKILL.md`)] })
      }
      const listings: Record<string, ReturnType<typeof file>[]> = {
        'workspace/': [file('workspace/held', true), file('workspace/site.html')],
        'workspace/held': [file('workspace/late.html')],
        '': [file('workspace/fresh.html')],
        '.crabbox': [file(`.crabbox/${owner}-fallback.txt`)],
        '.crabbox/captures': [file(`.crabbox/captures/${owner}-report.html`)],
      }
      expect(Object.hasOwn(listings, path)).toBe(true)
      return await route.fulfill({ json: listings[path] })
    } finally { hold?.finished.resolve() }
  })
  return {
    chat, requests,
    hold(key: string) {
      const hold = createHold()
      waiting.set(key, hold)
      holds.push(hold)
      return { requested: hold.requested.promise, release: async () => { hold.release.resolve(); await hold.finished.promise } }
    },
    releaseAll() { for (const hold of holds) hold.release.resolve() },
  }
}
