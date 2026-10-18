import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { listLocalDepartments, localDepartmentNameSchema, renameLocalDepartment } from '../src/lib/local-departments-api.ts'

test('options parses only safe summaries from the own-tenant endpoint', async (context) => {
  // Given
  const signal = new AbortController().signal
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/departments/options')
    assert.equal(init.signal, signal)
    return Response.json({ departments: [{ id: 'local', name: 'Engineering', tenant_id: 'private' }] })
  })
  // When
  const departments = await listLocalDepartments(signal)
  // Then
  assert.deepEqual(departments, [{ id: 'local', name: 'Engineering' }])
})

test('options rejects an invalid server response instead of rendering it', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ departments: [{ id: 'local' }] }))
  // When
  const response = listLocalDepartments(new AbortController().signal)
  // Then
  await assert.rejects(response, ZodError)
})

test('rename encodes the id and sends only the parsed name', async (context) => {
  // Given
  const name = localDepartmentNameSchema.parse('  Operations  ')
  const department = { id: 'local/one', name: 'Operations', tenant_id: 'tenant', created_at: '2026-10-18T00:00:00Z' }
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/departments/local%2Fone')
    assert.equal(init.method, 'PATCH')
    assert.equal(init.body, JSON.stringify({ name: 'Operations' }))
    return Response.json(department)
  })
  // When
  const renamed = await renameLocalDepartment(department.id, name)
  // Then
  assert.deepEqual(renamed, department)
})

for (const name of ['', '   ', 'x'.repeat(101)]) {
  test(`name parsing rejects invalid input of length ${name.length}`, () => {
    // Given / When
    const result = localDepartmentNameSchema.safeParse(name)
    // Then
    assert.equal(result.success, false)
  })
}

test('name parsing accepts the trimmed 100-character boundary', () => {
  // Given
  const name = 'x'.repeat(100)
  // When
  const parsed = localDepartmentNameSchema.parse(` ${name} `)
  // Then
  assert.equal(parsed, name)
})
