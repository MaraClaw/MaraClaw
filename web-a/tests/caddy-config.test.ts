import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'

const config = readFileSync(new URL('../docker/Caddyfile', import.meta.url), 'utf8')
const lines = config.split('\n').map((line) => line.replace(/#.*$/, '').trim())

/** Directive lines inside the block opened by `header {`, nested blocks included. */
function block(header: string): string[] {
  const start = lines.indexOf(`${header} {`)
  assert.notEqual(start, -1, `missing block: ${header}`)
  const body: string[] = []
  for (let depth = 1, index = start + 1; depth > 0; index += 1) {
    const line = lines[index]
    depth += (line.endsWith('{') ? 1 : 0) - (line === '}' ? 1 : 0)
    if (depth > 0 && line) body.push(line)
  }
  return body
}

/** The SPA catch-all's Content-Security-Policy as directive name -> source list. */
function contentSecurityPolicy(): Map<string, string> {
  const header = block('handle').find((line) => line.startsWith('Content-Security-Policy '))
  assert.ok(header, 'the SPA catch-all sets no Content-Security-Policy')
  const directives = header.slice('Content-Security-Policy '.length).replace(/^"|"$/g, '').split(';')
  return new Map(directives.map((directive) => directive.trim().split(/\s+/)).map(([name = '', ...sources]) => [name, sources.join(' ')]))
}

const evidencePattern = '^/api/capacity/orders/[0-9a-f-]+/evidence$'

test('production API requests reach the engine with their original URI and forwarded headers', () => {
  const proxy = block('(engine_proxy)')
  assert.ok(proxy.includes('reverse_proxy maraclaw-engine:8000 {'))
  assert.ok(proxy.includes('read_timeout {args[1]}'))
  // Caddy forwards Host and X-Forwarded-* by default; nothing may rewrite the URI or those headers.
  assert.ok(proxy.every((line) => !/^(rewrite|uri|handle_path|header_up)\b/.test(line)))
  assert.deepEqual(block('handle /api/*'), ['import engine_proxy api 120s'])
})

test('payment evidence uploads alone get the large body limit and otherwise proxy like any API call', () => {
  assert.ok(lines.includes(`@evidence path_regexp evidence ${evidencePattern}`))
  assert.ok(lines.includes(`@not_evidence not path_regexp ${evidencePattern}`))
  assert.deepEqual(block('request_body @not_evidence'), ['max_size 2MiB'])
  const evidence = block('handle @evidence')
  assert.ok(evidence.includes('max_size 11MiB'))
  assert.ok(evidence.includes('read_timeout 60s'))
  assert.equal(evidence.at(-1), block('handle /api/*').at(-1))
  assert.ok(block('timeouts').includes('read_body_idle 10s'))
})

test('production health probes retain their local success response', () => {
  assert.ok(block('handle /healthz').includes('respond "ok" 200'))
})

test('production client routes retain the SPA fallback', () => {
  const spa = block('handle')
  assert.ok(spa.includes('try_files {path} {path}/ /index.html'))
  assert.equal(spa.at(-1), 'file_server')
})

test('production theme and font bootstrap files revalidate on every load while hashed assets stay immutable', () => {
  assert.ok(lines.includes('@early path /theme-init.js /fonts/fonts.css'))
  for (const file of ['theme-init.js', 'fonts/fonts.css']) assert.ok(existsSync(new URL(`../public/${file}`, import.meta.url)), file)
  assert.deepEqual(block('handle @early'), ['log_skip', 'header Cache-Control no-cache', 'header X-Content-Type-Options nosniff', 'file_server'])
  assert.ok(lines.indexOf('handle @early {') < lines.indexOf('handle {'))
  assert.ok(block('handle /assets/*').includes('header Cache-Control "public, max-age=31536000, immutable"'))
  for (const handle of ['handle /assets/*', 'handle @static']) assert.ok(block(handle).includes('log_skip'), handle)
  assert.ok(block('handle').includes('Cache-Control "no-cache, no-store, must-revalidate"'))
  assert.ok(lines.includes('header -Server'))
})

test('production first paint needs nothing the CSP blocks: scripts, styles and fonts come from the app origin', () => {
  const policy = contentSecurityPolicy()
  assert.equal(policy.get('script-src'), "'self'")
  assert.equal(policy.get('style-src'), "'self' 'unsafe-inline'")
  assert.equal(policy.get('font-src'), "'self' data:")
  // The CSP allows no inline script, so the theme bootstrap is an external file reading the key use-theme.tsx writes.
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
  assert.deepEqual(Array.from(html.matchAll(/<script\b[^>]*>/g), (match) => /\ssrc="/.test(match[0])), [true, true])
  assert.ok(html.includes('<script src="/theme-init.js"></script>'))
  assert.ok(html.includes('<link rel="stylesheet" href="/fonts/fonts.css" />'))
  assert.doesNotMatch(html, /https?:\/\//)
  const storageKey = /const STORAGE_KEY = '([^']+)'/.exec(readFileSync(new URL('../src/hooks/use-theme.tsx', import.meta.url), 'utf8'))?.[1]
  assert.equal(storageKey, 'maraclaw-admin-theme')
  assert.ok(readFileSync(new URL('../public/theme-init.js', import.meta.url), 'utf8').includes(`var key = '${storageKey}'`))
})
