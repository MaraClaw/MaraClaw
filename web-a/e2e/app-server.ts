import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'

export async function startApp() {
  const work = await mkdtemp(join(tmpdir(), 'web-a-e2e-server-'))
  const blocked: string[] = []
  let server: ViteDevServer | undefined
  async function close() {
    try { await server?.close() }
    finally { await rm(work, { recursive: true, force: true }) }
  }
  try {
    server = await createServer({
      root: fileURLToPath(new URL('..', import.meta.url)),
      configLoader: 'runner',
      cacheDir: join(work, 'vite-cache'),
      server: { host: '127.0.0.1', port: 0, hmr: false, watch: { ignored: [work] } },
      plugins: [{
        name: 'selected-e2e-no-live-api',
        configureServer(instance) {
          instance.middlewares.use((request, response, next) => {
            if (!/^\/(api|ws|p)(\/|\?|$)/.test(request.url ?? '')) return next()
            blocked.push(request.url ?? '')
            response.writeHead(599, { 'Content-Type': 'application/json' })
            response.end(JSON.stringify({ detail: 'Synthetic API interception required' }))
          })
        },
      }],
    })
    await server.listen()
    const url = server.resolvedUrls?.local[0]
    if (!url) throw new Error('Vite did not publish an owned test URL')
    return { url, blocked, close }
  } catch (error) {
    await close()
    throw error
  }
}
