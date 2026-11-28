// Fails when the gzip size of what `dist/index.html` loads up front passes the package's committed budget.
// Usage (from a package directory, after `npm run build`): node ../.github/scripts/check-bundle-budget.mjs [dist] [budget.json]
// `entryGzipBytes` caps the entry module; `initialGzipBytes` caps the entry plus every modulepreload chunk,
// so a shared static import that drags a lazy chunk (a locale catalog, phone metadata) back in is caught too.
// Optional `routeGzipBytes` maps a route module's manifest key (`src/pages/overview.tsx`) to a cap on its chunk plus
// every chunk it statically imports, read from `dist/.vite/manifest.json` (`build.manifest`); dynamic imports load later.
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { gzipSync } from 'node:zlib'

const [distDir = 'dist', budgetPath = 'bundle-budget.json'] = process.argv.slice(2)

function tagAttributes(tag) {
  return Object.fromEntries(Array.from(tag.matchAll(/([\w-]+)="([^"]*)"/g), (match) => [match[1], match[2]]))
}

function gzipBytes(url) {
  return gzipSync(readFileSync(path.join(distDir, url)), { level: 9 }).length
}

const html = readFileSync(path.join(distDir, 'index.html'), 'utf8')
const tags = Array.from(html.matchAll(/<(?:script|link)\b[^>]*>/g), (match) => ({ name: match[0].startsWith('<script') ? 'script' : 'link', ...tagAttributes(match[0]) }))
const entries = tags.filter((tag) => tag.name === 'script' && tag.type === 'module' && tag.src !== undefined).map((tag) => tag.src)
const preloads = tags.filter((tag) => tag.name === 'link' && tag.rel === 'modulepreload' && tag.href !== undefined).map((tag) => tag.href)
if (entries.length !== 1) {
  console.error(`Expected exactly one module entry script in ${distDir}/index.html, found ${entries.length}`)
  process.exit(1)
}

function staticClosureGzipBytes(manifest, route) {
  const files = new Set()
  const visit = (key) => {
    const chunk = manifest[key]
    if (files.has(chunk.file)) return
    files.add(chunk.file)
    for (const imported of chunk.imports ?? []) visit(imported)
  }
  visit(route)
  return [...files].reduce((total, file) => total + gzipBytes(file), 0)
}

const budget = JSON.parse(readFileSync(budgetPath, 'utf8'))
const checks = [
  { name: 'entryGzipBytes', bytes: gzipBytes(entries[0]), limit: budget.entryGzipBytes },
  { name: 'initialGzipBytes', bytes: [...entries, ...preloads].reduce((total, url) => total + gzipBytes(url), 0), limit: budget.initialGzipBytes },
]
if (budget.routeGzipBytes !== undefined) {
  if (typeof budget.routeGzipBytes !== 'object' || budget.routeGzipBytes === null || Array.isArray(budget.routeGzipBytes)) {
    console.error(`${budgetPath} routeGzipBytes must map route modules to byte budgets`)
    process.exit(1)
  }
  const manifestPath = path.join(distDir, '.vite', 'manifest.json')
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
  for (const [route, limit] of Object.entries(budget.routeGzipBytes)) {
    if (manifest[route] === undefined) {
      console.error(`${manifestPath} has no module ${route} named in ${budgetPath} routeGzipBytes`)
      process.exit(1)
    }
    checks.push({ name: `routeGzipBytes ${route}`, bytes: staticClosureGzipBytes(manifest, route), limit })
  }
}

let failed = false
for (const { name, bytes, limit } of checks) {
  if (!Number.isInteger(limit) || limit <= 0) {
    console.error(`${budgetPath} needs a positive integer ${name}`)
    process.exit(1)
  }
  const over = bytes > limit
  failed ||= over
  console.log(`${over ? 'FAIL' : 'ok  '} ${name}: ${bytes} B gzip, budget ${limit} B`)
}
process.exit(failed ? 1 : 0)
