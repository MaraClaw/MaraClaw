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
    assert.ok(line !== undefined, `unterminated block: ${header}`)
    depth += (line.endsWith('{') ? 1 : 0) - (line === '}' ? 1 : 0)
    if (depth > 0 && line) body.push(line)
  }
  return body
}

test('production theme and font bootstrap files revalidate on every load while hashed assets stay immutable', () => {
  assert.ok(lines.includes('@early path /theme-init.js /fonts/fonts.css'))
  for (const file of ['theme-init.js', 'fonts/fonts.css']) assert.ok(existsSync(new URL(`../public/${file}`, import.meta.url)), file)
  assert.deepEqual(block('handle @early'), ['log_skip', 'header Cache-Control no-cache', 'header X-Content-Type-Options nosniff', 'file_server'])
  assert.ok(lines.indexOf('handle @early {') < lines.indexOf('handle {'))
  assert.ok(block('handle /assets/*').includes('header Cache-Control "public, max-age=31536000, immutable"'))
  for (const handle of ['handle /assets/*', 'handle @static']) assert.ok(block(handle).includes('log_skip'), handle)
  assert.ok(lines.includes('header -Server'))
})

test('production client routes keep the no-store HTML headers and the WebAssembly and worker CSP', () => {
  const spa = block('handle')
  assert.ok(spa.includes('Cache-Control "no-cache, no-store, must-revalidate"'))
  assert.ok(spa.includes('try_files {path} {path}/ /index.html'))
  const policy = spa.find((line) => line.startsWith('Content-Security-Policy '))
  assert.ok(policy?.includes("script-src 'self' 'wasm-unsafe-eval';"))
  assert.ok(policy?.includes("worker-src 'self' blob:;"))
  assert.ok(policy?.includes("font-src 'self' data:; style-src 'self' 'unsafe-inline';"))
})
