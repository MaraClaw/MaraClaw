// Fails when the gzip size of what `dist/index.html` loads up front passes the package's committed budget.
// Usage (from a package directory, after `npm run build`): node ../.github/scripts/check-bundle-budget.mjs [dist] [budget.json]
// `entryGzipBytes` caps the entry module; `initialGzipBytes` caps the entry plus every modulepreload chunk,
// so a shared static import that drags a lazy chunk (a locale catalog, phone metadata) back in is caught too.
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

const budget = JSON.parse(readFileSync(budgetPath, 'utf8'))
const measured = {
  entryGzipBytes: gzipBytes(entries[0]),
  initialGzipBytes: [...entries, ...preloads].reduce((total, url) => total + gzipBytes(url), 0),
}

let failed = false
for (const [name, bytes] of Object.entries(measured)) {
  const limit = budget[name]
  if (!Number.isInteger(limit) || limit <= 0) {
    console.error(`${budgetPath} needs a positive integer ${name}`)
    process.exit(1)
  }
  const over = bytes > limit
  failed ||= over
  console.log(`${over ? 'FAIL' : 'ok  '} ${name}: ${bytes} B gzip, budget ${limit} B`)
}
process.exit(failed ? 1 : 0)
