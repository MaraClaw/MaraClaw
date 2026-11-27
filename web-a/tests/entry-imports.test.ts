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

test('Given the entry module graph, validation, data fetching, motion, class merging and date formatting load only through dynamic imports', () => {
  // When
  const { files, packages } = eagerImports(path.join(src, 'main.tsx'))
  // Then: the walk reaches the session store and auth provider, and nothing that parses responses, queries, animates, merges classes or formats dates.
  for (const kept of ['hooks/use-auth.tsx', 'lib/auth-session.ts']) {
    assert.ok(files.has(path.join(src, kept)), `${kept} is not reached from main.tsx`)
  }
  const heavy = ['zod', '@tanstack/react-query', 'framer-motion', 'motion', 'motion-dom', 'date-fns', 'tailwind-merge']
  assert.deepEqual([...packages].filter((name) => heavy.some((pkg) => name === pkg || name.startsWith(`${pkg}/`))), [])
  for (const lazy of ['lib/auth-api.ts', 'lib/types/auth.ts', 'lib/engine-contracts.generated.ts', 'components/layout/admin-shell.tsx']) {
    assert.equal(files.has(path.join(src, lazy)), false, `${lazy} is a static import of main.tsx`)
  }
})
