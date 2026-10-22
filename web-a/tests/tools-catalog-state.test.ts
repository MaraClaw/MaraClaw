import assert from 'node:assert/strict'
import test from 'node:test'

import { toolActions, toolOwnership } from '../src/pages/tools-catalog-state.ts'

for (const [source, tenant_id, expected] of [
  ['builtin', null, 'Built-in'],
  ['builtin', 'selected-company', 'Built-in'],
  ['custom', null, 'Global'],
  ['custom', 'other-company', 'Company'],
] as const) {
  test(`ownership distinguishes ${source} records with ${tenant_id ?? 'null'} tenant`, () => {
    // Given / When
    const ownership = toolOwnership({ source, tenant_id })
    // Then
    assert.equal(ownership, expected)
  })
}

for (const can_edit_details of [false, true]) {
  for (const can_delete of [false, true]) {
    for (const can_configure of [false, true]) {
      test(`MCP actions honor independent capability flags ${can_edit_details}/${can_delete}/${can_configure}`, () => {
        // Given
        const tool = { type: 'mcp', capabilities: { can_edit_details, can_delete, can_configure } }
        // When
        const actions = toolActions(tool, true)
        // Then
        assert.deepEqual(actions, { editDetails: can_edit_details, delete: can_delete, configure: can_configure })
      })
    }
  }
}

test('current scope prevents configuration without suppressing server-permitted metadata or deletion', () => {
  // Given
  const tool = { type: 'mcp', capabilities: { can_edit_details: true, can_delete: true, can_configure: true } }
  // When
  const actions = toolActions(tool, false)
  // Then
  assert.deepEqual(actions, { editDetails: true, delete: true, configure: false })
})

test('built-in deletion stays hidden even if a capability permits it', () => {
  // Given
  const tool = { type: 'builtin', capabilities: { can_edit_details: false, can_delete: true, can_configure: true } }
  // When
  const actions = toolActions(tool, true)
  // Then
  assert.deepEqual(actions, { editDetails: false, delete: false, configure: true })
})
