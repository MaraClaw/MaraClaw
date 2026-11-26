import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'

const script = fileURLToPath(new URL('../../.github/scripts/check-bundle-budget.mjs', import.meta.url))
const entrySource = 'export const entry = ' + JSON.stringify(Array.from({ length: 400 }, (_, index) => `entry-${index * 7919 % 1009}`))
const preloadSource = 'export const shared = ' + JSON.stringify(Array.from({ length: 400 }, (_, index) => `shared-${index * 104729 % 997}`))
const gzip = (source: string) => gzipSync(source, { level: 9 }).length

function check(budget: { entryGzipBytes: number; initialGzipBytes: number }) {
  const root = mkdtempSync(path.join(tmpdir(), 'bundle-budget-'))
  try {
    mkdirSync(path.join(root, 'dist', 'assets'), { recursive: true })
    writeFileSync(path.join(root, 'dist', 'index.html'), [
      '<script type="module" crossorigin src="/assets/index.js"></script>',
      '<link rel="modulepreload" crossorigin href="/assets/shared.js">',
    ].join('\n'))
    writeFileSync(path.join(root, 'dist', 'assets', 'index.js'), entrySource)
    writeFileSync(path.join(root, 'dist', 'assets', 'shared.js'), preloadSource)
    writeFileSync(path.join(root, 'budget.json'), JSON.stringify(budget))
    return spawnSync(process.execPath, [script, path.join(root, 'dist'), path.join(root, 'budget.json')], { encoding: 'utf8' })
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

test('Given budgets at the measured sizes, the check passes', () => {
  const result = check({ entryGzipBytes: gzip(entrySource), initialGzipBytes: gzip(entrySource) + gzip(preloadSource) })
  assert.equal(result.status, 0, result.stdout + result.stderr)
})

test('Given an entry chunk one byte over its budget, the check fails', () => {
  const result = check({ entryGzipBytes: gzip(entrySource) - 1, initialGzipBytes: gzip(entrySource) + gzip(preloadSource) })
  assert.equal(result.status, 1)
  assert.match(result.stdout, /FAIL entryGzipBytes/)
})

test('Given preloaded chunks that push the initial load over its budget, the check fails', () => {
  const result = check({ entryGzipBytes: gzip(entrySource), initialGzipBytes: gzip(entrySource) + gzip(preloadSource) - 1 })
  assert.equal(result.status, 1)
  assert.match(result.stdout, /FAIL initialGzipBytes/)
})
