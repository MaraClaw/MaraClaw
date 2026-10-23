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
