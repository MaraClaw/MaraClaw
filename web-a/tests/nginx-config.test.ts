import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('production API requests reach the engine with their original URI and forwarded headers', () => {
  // Given
  const config = readFileSync(new URL('../docker/nginx.conf', import.meta.url), 'utf8')
  // When
  const apiBlock = config.match(/^\s*location\s+\/api\/\s*\{([^{}]*)\}/m)?.[1]
  // Then
  assert.ok(apiBlock, 'production nginx must route /api/ requests to the engine')
  assert.match(apiBlock, /^\s*proxy_pass\s+http:\/\/maraclaw-engine:8000\s*;\s*$/m)
  assert.match(apiBlock, /^\s*proxy_http_version\s+1\.1\s*;/m)
  assert.match(apiBlock, /^\s*proxy_set_header\s+Host\s+\$host\s*;/m)
  assert.match(apiBlock, /^\s*proxy_set_header\s+X-Forwarded-For\s+\$proxy_add_x_forwarded_for\s*;/m)
  assert.match(apiBlock, /^\s*proxy_set_header\s+X-Forwarded-Proto\s+\$scheme\s*;/m)
})

test('payment evidence uploads alone get the large body limit and otherwise proxy like any API call', () => {
  // Given
  const config = readFileSync(new URL('../docker/nginx.conf', import.meta.url), 'utf8')
  const apiBlock = config.match(/^\s*location\s+\/api\/\s*\{([^{}]*)\}/m)?.[1]
  // When
  const evidenceBlock = config.match(/^\s*location\s+~\s+\^\/api\/capacity\/orders\/\[0-9a-f-\]\+\/evidence\$\s*\{([^{}]*)\}/m)?.[1]
  const proxyLines = (block: string) => block.split('\n').map((line) => line.trim())
    .filter((line) => /^(proxy_|set\s+\$maraclaw_route)/.test(line))
  // Then
  assert.ok(apiBlock)
  assert.ok(evidenceBlock, 'the evidence upload needs its own location')
  assert.match(evidenceBlock, /^\s*client_max_body_size\s+11m\s*;/m)
  assert.match(evidenceBlock, /^\s*client_body_timeout\s+60s\s*;/m)
  assert.deepEqual(proxyLines(evidenceBlock), proxyLines(apiBlock))
  assert.doesNotMatch(apiBlock, /client_max_body_size|client_body_timeout/)
  assert.match(config, /^ {4}client_max_body_size\s+2m\s*;/m)
  assert.match(config, /^ {4}client_body_timeout\s+10s\s*;/m)
})

test('production health probes retain their local success response', () => {
  // Given
  const config = readFileSync(new URL('../docker/nginx.conf', import.meta.url), 'utf8')
  // When
  const healthBlock = config.match(/^\s*location\s+=\s+\/healthz\s*\{([^{}]*)\}/m)?.[1]
  // Then
  assert.ok(healthBlock)
  assert.match(healthBlock, /^\s*return\s+200\s+"ok\\n"\s*;/m)
})

test('production client routes retain the SPA fallback', () => {
  // Given
  const config = readFileSync(new URL('../docker/nginx.conf', import.meta.url), 'utf8')
  // When
  const spaBlock = config.match(/^\s*location\s+\/\s*\{([^{}]*)\}/m)?.[1]
  // Then
  assert.ok(spaBlock)
  assert.match(spaBlock, /^\s*try_files\s+\$uri\s+\$uri\/\s+\/index\.html\s*;/m)
})
