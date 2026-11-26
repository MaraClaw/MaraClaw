import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const src = fileURLToPath(new URL('../src/', import.meta.url))

function resolveSource(from: string, specifier: string): string | null {
  const base = specifier.startsWith('@/') ? path.join(src, specifier.slice(2)) : path.resolve(path.dirname(from), specifier)
  return [base, `${base}.ts`, `${base}.tsx`, path.join(base, 'index.ts'), path.join(base, 'index.tsx')]
    .find((file) => /\.tsx?$/.test(file) && existsSync(file)) ?? null
}

/** Source files and packages evaluated before any dynamic `import()`. Only `import type` / `export type` are skipped. */
function eagerImports(entry: string) {
  const files = new Set<string>()
  const packages = new Set<string>()
  const visit = (file: string): void => {
    if (files.has(file)) return
    files.add(file)
    for (const statement of ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest).statements) {
      if (!ts.isImportDeclaration(statement) && !ts.isExportDeclaration(statement)) continue
      const typeOnly = ts.isImportDeclaration(statement) ? statement.importClause?.isTypeOnly === true : statement.isTypeOnly
      const specifier = statement.moduleSpecifier
      if (typeOnly || specifier === undefined || !ts.isStringLiteral(specifier)) continue
      if (!specifier.text.startsWith('.') && !specifier.text.startsWith('@/')) packages.add(specifier.text)
      const target = packages.has(specifier.text) ? null : resolveSource(file, specifier.text)
      if (target) visit(target)
    }
  }
  visit(entry)
  return { files, packages }
}

test('Given the entry module graph, zod, the generated contracts and the auth client load only through dynamic imports', () => {
  // When
  const { files, packages } = eagerImports(path.join(src, 'main.tsx'))
  // Then: the walk reaches the entry's session store and auth provider, and nothing that parses responses with zod.
  assert.ok(files.has(path.join(src, 'hooks/auth-provider.tsx')))
  assert.ok(files.has(path.join(src, 'lib/auth-session.ts')))
  assert.deepEqual([...packages].filter((name) => name === 'zod' || name.startsWith('zod/')), [])
  for (const lazy of ['lib/auth-api.ts', 'lib/types/auth.ts', 'lib/engine-contracts.generated.ts']) {
    assert.equal(files.has(path.join(src, lazy)), false, `${lazy} is a static import of main.tsx`)
  }
})
