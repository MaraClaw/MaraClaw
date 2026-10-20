import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ZodError } from 'zod'

import { listOrgDepartmentMembers, listOrgDepartments, orgDepartmentNameSchema, renameOrgDepartment } from '../src/lib/org-departments-api.ts'

test('options parses only safe summaries from the own-tenant endpoint', async (context) => {
  // Given
  const signal = new AbortController().signal
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/departments/options')
    assert.equal(init.signal, signal)
    return Response.json({ departments: [{ id: 'local', name: 'Engineering', tenant_id: 'private' }] })
  })
  // When
  const departments = await listOrgDepartments(signal)
  // Then
  assert.deepEqual(departments, [{ id: 'local', name: 'Engineering' }])
})

test('options rejects an invalid server response instead of rendering it', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ departments: [{ id: 'local' }] }))
  // When
  const response = listOrgDepartments(new AbortController().signal)
  // Then
  await assert.rejects(response, ZodError)
})

test('members encodes the id, forwards the signal, and strips private fields without merging duplicate names', async (context) => {
  // Given
  const signal = new AbortController().signal
  const members = [
    { membership_id: '0c929328-fb2a-4b51-83fc-284bf606a040', display_name: 'Alex' },
    { membership_id: '91efb1f0-503b-4bb4-8a40-4792f8a8c029', display_name: 'Alex' },
    { membership_id: '169eb789-1689-49c0-aa96-5da877a58036', display_name: null },
  ]
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/departments/local%2Fone%3F%23/members')
    assert.equal(init.signal, signal)
    return Response.json({ members: members.map((member) => ({
      ...member, email: 'private@example.test', username: 'private', user_id: 'private', role: 'member', tenant_id: 'private',
    })) })
  })
  // When
  const roster = await listOrgDepartmentMembers('local/one?#', signal)
  // Then
  assert.deepEqual(roster, members)
})

for (const response of [
  {}, { members: null }, { members: [{ membership_id: 'not-a-uuid', display_name: 'Alex' }] },
  { members: [{ display_name: 'Alex' }] },
  { members: [{ membership_id: '0c929328-fb2a-4b51-83fc-284bf606a040', email: 'private@example.test' }] },
  { members: [{ membership_id: '0c929328-fb2a-4b51-83fc-284bf606a040', display_name: 42 }] },
]) {
  test(`members rejects malformed roster ${JSON.stringify(response)}`, async (context) => {
    // Given
    context.mock.method(globalThis, 'fetch', async () => Response.json(response))
    // When
    const roster = listOrgDepartmentMembers('local', new AbortController().signal)
    // Then
    await assert.rejects(roster, ZodError)
  })
}

test('members accepts an empty roster', async (context) => {
  // Given
  context.mock.method(globalThis, 'fetch', async () => Response.json({ members: [] }))
  // When
  const roster = await listOrgDepartmentMembers('local', new AbortController().signal)
  // Then
  assert.deepEqual(roster, [])
})

test('members discards a response when its forwarded signal is aborted', async (context) => {
  // Given
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async () => {
    controller.abort()
    return Response.json({ members: [] })
  })
  // When
  const roster = listOrgDepartmentMembers('local', controller.signal)
  // Then
  await assert.rejects(roster, { name: 'AbortError' })
})

test('rename encodes the id and sends only the parsed name', async (context) => {
  // Given
  const name = orgDepartmentNameSchema.parse('  Operations  ')
  const department = { id: 'local/one', name: 'Operations', tenant_id: 'tenant', created_at: '2026-10-18T00:00:00Z' }
  context.mock.method(globalThis, 'fetch', async (path: string, init: RequestInit) => {
    assert.equal(path, '/api/departments/local%2Fone')
    assert.equal(init.method, 'PATCH')
    assert.equal(init.body, JSON.stringify({ name: 'Operations' }))
    return Response.json(department)
  })
  // When
  const renamed = await renameOrgDepartment(department.id, name)
  // Then
  assert.deepEqual(renamed, department)
})

for (const name of ['', '   ', 'x'.repeat(101)]) {
  test(`name parsing rejects invalid input of length ${name.length}`, () => {
    // Given / When
    const result = orgDepartmentNameSchema.safeParse(name)
    // Then
    assert.equal(result.success, false)
  })
}

test('name parsing accepts the trimmed 100-character boundary', () => {
  // Given
  const name = 'x'.repeat(100)
  // When
  const parsed = orgDepartmentNameSchema.parse(` ${name} `)
  // Then
  assert.equal(parsed, name)
})
