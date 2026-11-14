import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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
