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
const routeSource = 'export const route = ' + JSON.stringify(Array.from({ length: 400 }, (_, index) => `route-${index * 7907 % 991}`))
const chartSource = 'export const chart = ' + JSON.stringify(Array.from({ length: 400 }, (_, index) => `chart-${index * 7901 % 983}`))
const gzip = (source: string) => gzipSync(source, { level: 9 }).length
// The route statically imports the entry and the shared chunk (counted once) and loads the chart only through `import()`.
const manifest = {
  'index.html': { file: 'assets/index.js', isEntry: true, imports: ['_shared.js'], dynamicImports: ['src/pages/report.tsx'] },
  '_shared.js': { file: 'assets/shared.js' },
  'src/pages/report.tsx': { file: 'assets/report.js', isDynamicEntry: true, imports: ['index.html', '_shared.js'], dynamicImports: ['src/pages/chart.tsx'] },
  'src/pages/chart.tsx': { file: 'assets/chart.js', isDynamicEntry: true, imports: ['index.html'] },
}
const reportClosure = gzip(routeSource) + gzip(entrySource) + gzip(preloadSource)

function check(budget: { entryGzipBytes: number; initialGzipBytes: number; routeGzipBytes?: Record<string, number> }) {
  const root = mkdtempSync(path.join(tmpdir(), 'bundle-budget-'))
  try {
    mkdirSync(path.join(root, 'dist', 'assets'), { recursive: true })
    mkdirSync(path.join(root, 'dist', '.vite'))
    writeFileSync(path.join(root, 'dist', 'index.html'), [
      '<script type="module" crossorigin src="/assets/index.js"></script>',
      '<link rel="modulepreload" crossorigin href="/assets/shared.js">',
    ].join('\n'))
    writeFileSync(path.join(root, 'dist', 'assets', 'index.js'), entrySource)
    writeFileSync(path.join(root, 'dist', 'assets', 'shared.js'), preloadSource)
    writeFileSync(path.join(root, 'dist', 'assets', 'report.js'), routeSource)
    writeFileSync(path.join(root, 'dist', 'assets', 'chart.js'), chartSource)
    writeFileSync(path.join(root, 'dist', '.vite', 'manifest.json'), JSON.stringify(manifest))
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

test('Given a route budget at its static import closure, the check passes without counting dynamic imports', () => {
  const result = check({
    entryGzipBytes: gzip(entrySource),
    initialGzipBytes: gzip(entrySource) + gzip(preloadSource),
    routeGzipBytes: { 'src/pages/report.tsx': reportClosure },
  })
  assert.equal(result.status, 0, result.stdout + result.stderr)
  assert.match(result.stdout, new RegExp(`ok   routeGzipBytes src/pages/report.tsx: ${reportClosure} B gzip`))
})

test('Given a route whose static import closure is one byte over its budget, the check fails', () => {
  const result = check({
    entryGzipBytes: gzip(entrySource),
    initialGzipBytes: gzip(entrySource) + gzip(preloadSource),
    routeGzipBytes: { 'src/pages/report.tsx': reportClosure - 1 },
  })
  assert.equal(result.status, 1)
  assert.match(result.stdout, /FAIL routeGzipBytes src\/pages\/report\.tsx/)
})

test('Given a route budget for a module the build does not have, the check fails', () => {
  const result = check({
    entryGzipBytes: gzip(entrySource),
    initialGzipBytes: gzip(entrySource) + gzip(preloadSource),
    routeGzipBytes: { 'src/pages/renamed.tsx': reportClosure },
  })
  assert.equal(result.status, 1)
  assert.match(result.stderr, /has no module src\/pages\/renamed\.tsx/)
})
